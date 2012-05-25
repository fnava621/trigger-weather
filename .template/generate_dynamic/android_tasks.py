from collections import namedtuple
from getpass import getpass
import logging
import multiprocessing
import os
from os import path
import re
import shutil
from subprocess import PIPE, STDOUT
import threading
import sys
import tempfile
import time
import urllib
import zipfile

import lib
from lib import temp_file, task, CouldNotLocate
from utils import run_shell

LOG = logging.getLogger(__name__)

class AndroidError(lib.BASE_EXCEPTION):
	pass

PathInfo = namedtuple('PathInfo', 'android adb aapt sdk')

def _run_adb(cmd, timeout, path_info):
	runner = {
		"process": None,
		"std_out": None
	}
	def target():
		try:
			runner['process'] = lib.PopenWithoutNewConsole(cmd, stdout=PIPE, stderr=STDOUT)
		except Exception:
			LOG.error("problem finding the android debug bridge at: %s" % path_info.adb)
			# XXX: prompt to run the sdk manager, then retry?
			LOG.error("this probably means you need to run the Android SDK manager and download the Android platform-tools.")
			raise AndroidError

		runner['std_out'] = runner['process'].communicate()[0]

	thread = threading.Thread(target=target)
	thread.start()

	thread.join(timeout)
	if thread.is_alive():
		LOG.debug('ADB hung, terminating process')
		_restart_adb(path_info)
		thread.join()
	
	if runner['process'].returncode != 0:
		LOG.error('Communication with adb failed: %s' % (runner['std_out']))
		raise AndroidError
	
	return runner['std_out']

def _kill_adb():
	if sys.platform.startswith('win'):
		run_shell('taskkill', '/T', '/IM', 'adb.exe', fail_silently=True)
		run_shell('taskkill', '/T', '/F', '/IM', 'adb.exe', fail_silently=True)
	else:
		run_shell('killall', 'adb', fail_silently=True)
		run_shell('killall', '-9', 'adb', fail_silently=True)

def _restart_adb(path_info):
	# Force restart of adb
	_kill_adb()
	
	run_detached([path_info.adb, 'start-server'], wait=True)

def _look_for_java():
	possible_jre_locations = [
		r"C:\Program Files\Java\jre7",
		r"C:\Program Files\Java\jre6",
		r"C:\Program Files (x86)\Java\jre7",
		r"C:\Program Files (x86)\Java\jre6",
	]

	return [directory for directory in possible_jre_locations if path.isdir(directory)]

def _download_sdk_for_windows(temp_d):
	archive_path = path.join(temp_d, "sdk.zip")
	urllib.urlretrieve("https://trigger.io/redirect/android/windows", archive_path)

	LOG.info('Download complete, extracting SDK')
	zip_to_extract = zipfile.ZipFile(archive_path)
	zip_to_extract.extractall("C:\\")
	zip_to_extract.close()

	# XXX: should this really be hardcoded to C:\android-sdk-windows? wasn't sure if we were allowing user to specify location..
	return PathInfo(android=r"C:\android-sdk-windows\tools\android.bat", adb=r"C:\android-sdk-windows\platform-tools\adb", aapt=r"C:\android-sdk-windows\platform-tools\aapt", sdk=r"C:\android-sdk-windows")

def _download_sdk_for_mac(temp_d):
	archive_path = path.join(temp_d, "sdk.zip")
	urllib.urlretrieve("https://trigger.io/redirect/android/macosx", archive_path)

	LOG.info('Download complete, extracting SDK')
	zip_process = lib.PopenWithoutNewConsole(["unzip", archive_path, '-d', "/Applications"], stdout=PIPE, stderr=STDOUT)
	output = zip_process.communicate()[0]
	LOG.debug("unzip output")
	LOG.debug(output)

	return PathInfo(android="/Applications/android-sdk-macosx/tools/android", adb="/Applications/android-sdk-macosx/platform-tools/adb", aapt="/Applications/android-sdk-macosx/platform-tools/aapt", sdk="/Applications/android-sdk-macosx")

def _download_sdk_for_linux(temp_d):
	archive_path = path.join(temp_d, "sdk.tgz")
	urllib.urlretrieve("https://trigger.io/redirect/android/linux", archive_path)

	LOG.info('Download complete, extracting SDK')
	if not path.isdir(path.expanduser("~/.forge")):
		os.mkdir(path.expanduser("~/.forge"))

	zip_process = lib.PopenWithoutNewConsole(["tar", "zxf", archive_path, "-C", path.expanduser("~/.forge")], stdout=PIPE, stderr=STDOUT)
	output = zip_process.communicate()[0]
	LOG.debug("unzip output")
	LOG.debug(output)

	return PathInfo(
		android=path.expanduser("~/.forge/android-sdk-linux/tools/android"),
		adb=path.expanduser("~/.forge/android-sdk-linux/platform-tools/adb"),
		aapt=path.expanduser("~/.forge/android-sdk-linux/platform-tools/aapt"),
		sdk=path.expanduser("~/.forge/android-sdk-linux"),
	)

def _install_sdk_automatically():
	# Attempt download
	temp_d = tempfile.mkdtemp()
	try:
		LOG.info('Downloading Android SDK (about 30MB, may take some time)')
		if sys.platform.startswith('win'):
			path_info = _download_sdk_for_windows(temp_d)
		elif sys.platform.startswith('darwin'):
			path_info = _download_sdk_for_mac(temp_d)
		elif sys.platform.startswith('linux'):
			path_info = _download_sdk_for_linux(temp_d)

		_update_sdk(path_info)
	except Exception, e:
		LOG.error(e)
		raise CouldNotLocate("Automatic SDK download failed, please install manually and specify with the --android.sdk flag")
	else:
		LOG.info('Android SDK update complete')
		return _check_for_sdk()
	finally:
		shutil.rmtree(temp_d, ignore_errors=True)

def _update_sdk(path_info):
	LOG.info('Updating SDK and downloading required Android platform (about 90MB, may take some time)')
	with open(os.devnull, 'w') as devnull:
		android_process = lib.PopenWithoutNewConsole(
			[path_info.android, "update", "sdk", "--no-ui", "--filter", "platform-tool,tool,android-8"],
			stdout=devnull,
			stderr=devnull,
		)
		while android_process.poll() is None:
			time.sleep(5)
			try:
				lib.PopenWithoutNewConsole([path_info.adb, "kill-server"], stdout=devnull, stderr=devnull)
			except Exception:
				pass

def _should_install_sdk(sdk_path):
	resp = raw_input('''
No Android SDK found, would you like to:

(1) Attempt to download and install the SDK automatically to {sdk_path}, or,
(2) Install the SDK yourself and rerun this command with the --android.sdk option to specify its location.

Please enter 1 or 2: '''.format(sdk_path=sdk_path))

	return resp == "1"

def _prompt_user_to_attach_device(path_info):
	"Prompt to automatically (create and) run an AVD"
	prompt = raw_input('''
No active Android device found, would you like to:

(1) Attempt to automatically launch the Android emulator
(2) Attempt to find the device again (choose this option after plugging in an Android device or launching the emulator).

Please enter 1 or 2: ''')

	if not prompt == "1":
		return

	_create_avd_if_necessary(path_info)
	_launch_avd(path_info)

def _check_for_sdk(dir=None, interactive=True):
	# Some sensible places to look for the Android SDK
	possible_sdk = [
		"C:/Program Files (x86)/Android/android-sdk/",
		"C:/Program Files/Android/android-sdk/",
		"C:/Android/android-sdk/",
		"C:/Android/android-sdk-windows/",
		"C:/android-sdk-windows/",
		"/Applications/android-sdk-macosx",
		path.expanduser("~/.forge/android-sdk-linux")
	]
	if dir:
		possible_sdk.insert(0, dir)

	for directory in possible_sdk:
		if path.isdir(directory):
			return directory if directory.endswith('/') else directory+'/'
	else:
		# No SDK found - will the user let us install one?
		sdk_path = None
		
		if sys.platform.startswith('win'):
			sdk_path = "C:\\android-sdk-windows"
		elif sys.platform.startswith('linux'):
			sdk_path = path.expanduser("~/.forge/android-sdk-linux")
		elif sys.platform.startswith('darwin'):
			sdk_path = "/Applications/android-sdk-macosx"
			
		if not sdk_path:
			raise CouldNotLocate("No Android SDK found, please specify with the --android.sdk flag")
		
		if interactive:
			if _should_install_sdk(sdk_path):
				return _install_sdk_automatically()
			else:
				raise CouldNotLocate("No Android SDK found: please install one and use the --android.sdk flag")
		else:
			raise AndroidError("No Android SDK found, please specify one in your global settings")

def _scrape_available_devices(text):
	'Scrapes the output of the adb devices command into a list'
	lines = text.split('\n')
	available_devices = []

	for line in lines:
		words = line.split('\t')

		if len(words[0]) > 5 and words[0].find(" ") == -1:
			available_devices.append(words[0])

	return available_devices

def run_detached(args, wait=True):
	'''Run a process entirely detached from this one, and optionally wait
	for it to finish.

	:param args: list of shell arguments
	:param wait: don't return until the command completes
	'''
	if sys.platform.startswith('win'):
		if wait:
			os.system("cmd /c start /WAIT "
					"\"Detached Forge command - will automatically close\" "
					"\""+"\" \"".join(args)+"\"")
		else:
			os.system("cmd /c start "
					"\"Detached Forge command\" "
					"\""+"\" \"".join(args)+"\"")
	else:
		def run_in_shell(queue):
			'''will be invoked in by a separate process, to actually run the
			detached command'''
			# setsid detaches us completely from the caller
			os.setsid()

			# os.devnull is used to ensure that no [1] foo;
			# lines are shown in the commandline output
			with open(os.devnull) as devnull:
				proc = lib.PopenWithoutNewConsole(args, stdout=devnull, stderr=STDOUT)
			proc.wait()

			# signal that we're finished
			queue.put(True)

		# to get the "finished" signal
		queue = multiprocessing.Queue()

		# multiprocessing throws an error on start() if we spawn a process from
		# a daemon process but we don't care about this
		multiprocessing.current_process().daemon = False

		proc = multiprocessing.Process(target=run_in_shell, args=(queue, ))
		proc.daemon = True
		proc.start()
		# wait until the command completes
		queue.get()

def check_for_java():
	'Return True java exists on the path and can be invoked; False otherwise'
	with open(os.devnull, 'w') as devnull:
		try:
			proc = lib.PopenWithoutNewConsole(['java', '-version'], stdout=devnull, stderr=devnull)
			proc.communicate()[0]
			return proc.returncode == 0
		except:
			return False

def _create_avd(path_info):
	LOG.info('Creating AVD')
	args = [
		path_info.android,
		"create",
		"avd",
		"-n", "forge",
		"-t", "android-8",
		"--skin", "HVGA",
		"-p", path.join(path_info.sdk, 'forge-avd'),
		#"-a",
		"-c", "32M",
		"--force"
	]
	proc = lib.PopenWithoutNewConsole(args, stdin=PIPE, stdout=PIPE, stderr=STDOUT)
	time.sleep(0.1)
	proc_std = proc.communicate(input='\n')[0]
	if proc.returncode != 0:
		LOG.error('failed: %s' % (proc_std))
		raise AndroidError
	LOG.debug('Output:\n'+proc_std)

def _launch_avd(path_info):
	run_detached(
			[path.join(path_info.sdk, "tools", "emulator"),
				"-avd", "forge"],
			wait=False)
	
	LOG.info("Started emulator, waiting for device to boot")
	_run_adb([path_info.adb, 'wait-for-device'], 120, path_info)
	# adb shell can return too quickly without a small sleep here.
	time.sleep(1)
	_run_adb([path_info.adb, "shell", "pm", "path", "android"], 120, path_info)

def _create_apk_with_aapt(out_apk_name, path_info, package_name, lib_path, dev_dir):
	LOG.info('Creating APK with aapt')

	run_shell(path_info.aapt,
		'p', # create APK package
		'-F', out_apk_name, # output name
		'-S', path.join(dev_dir, 'res'), # uncompressed resources folder
		'-M', path.join(dev_dir, 'AndroidManifest.xml'), # uncompressed xml manifest
		'-I', path.join(lib_path, 'android-platform.apk'), # Android platform to "compile" resources against
		'-A', path.join(dev_dir, 'assets'), # Assets folder to include
		'-0', '', # Don't compress any assets - Important for content provider access to assets
		'--rename-manifest-package', package_name, # Package name
		'-f', # Force overwrite
		path.join(dev_dir, 'output'), # Location of raw files (app binary)
		command_log_level=logging.DEBUG)

def _sign_zipf(lib_path, jre, keystore, storepass, keyalias, keypass, signed_zipf_name, zipf_name):
	args = [
		path.join(jre,'java'),
		'-jar',
		path.join(lib_path, 'apk-signer.jar'),
		'--keystore',
		keystore,
		'--storepass',
		storepass,
		'--keyalias',
		keyalias,
		'--keypass',
		keypass,
		'--out',
		signed_zipf_name,
		zipf_name
	]
	run_shell(*args)

def _sign_zipf_debug(lib_path, jre, zipf_name, signed_zipf_name):
	LOG.info('Signing APK with a debug key')

	return _sign_zipf(
		lib_path=lib_path,
		jre=jre,
		keystore=path.join(lib_path, 'debug.keystore'),
		storepass="android",
		keyalias="androiddebugkey",
		keypass="android",
		signed_zipf_name=signed_zipf_name,
		zipf_name=zipf_name,
	)

def _sign_zipf_release(lib_path, jre, zipf_name, signed_zipf_name, keystore, storepass, keyalias, keypass):
	LOG.info('Signing APK with your release key')
	return _sign_zipf(
		lib_path=lib_path,
		jre=jre,
		keystore=keystore,
		storepass=storepass,
		keyalias=keyalias,
		keypass=keypass,
		signed_zipf_name=signed_zipf_name,
		zipf_name=zipf_name,
	)
	
def _align_apk(sdk, signed_zipf_name, out_apk_name):
	LOG.info('Aligning apk')

	args = [path.join(sdk, 'tools', 'zipalign'), '-v', '4', signed_zipf_name, out_apk_name]
	run_shell(*args)

def _generate_package_name(build):
	if "package_names" not in build.config["modules"]:
		build.config["modules"]["package_names"] = {}
	if "android" not in build.config["modules"]["package_names"]:
		build.config["modules"]["package_names"]["android"] = "io.trigger.forge"+build.config["uuid"]
	return build.config["modules"]["package_names"]["android"]
	
def _follow_log(path_info, chosen_device):
	LOG.info('Clearing android log')

	args = [path_info.adb, '-s', chosen_device, 'logcat', '-c']
	run_shell(*args, command_log_level=logging.INFO)

	LOG.info('Showing android log')

	run_shell(path_info.adb, '-s', chosen_device, 'logcat', 'WebCore:D', 'Forge:D', '*:s', command_log_level=logging.INFO)

def _create_avd_if_necessary(path_info):
	# Create avd
	LOG.info('Checking for previously created AVD')
	if path.isdir(path.join(path_info.sdk, 'forge-avd')):
		LOG.info('Existing AVD found')
	else:
		_create_avd(path_info)

def _create_path_info_from_sdk(sdk):
	return PathInfo(
		android=path.abspath(path.join(
			sdk,
			'tools',
			'android.bat' if sys.platform.startswith('win') else 'android'
		)),
		adb=path.abspath(path.join(sdk, 'platform-tools', 'adb')),
		aapt=path.abspath(path.join(sdk, 'platform-tools', 'aapt')),
		sdk=sdk,
	)
	
def _get_available_devices(path_info, try_count=0):
	proc_std = _run_adb([path_info.adb, 'devices'], timeout=10, path_info=path_info)
		
	available_devices = _scrape_available_devices(proc_std)
	
	if not available_devices and try_count < 3:
		LOG.debug('No devices found, checking again')
		time.sleep(2)
		if try_count == 1:
			_restart_adb(path_info)
		return _get_available_devices(path_info, (try_count+1))
	else:
		return available_devices

@task
def clean_android(build):
	pass

@task
def run_android(build, build_type_dir, sdk, device, interactive=True, purge=False):
	if sdk:
		sdk = path.normpath(path.join(build.orig_wd, sdk))
	sdk = _check_for_sdk(sdk, interactive=interactive)
	jre = ""

	if not check_for_java():
		jres = _look_for_java()
		if not jres:
			raise AndroidError("Java not found: Java must be installed and available in your path in order to run Android")
		jre = path.join(jres[0], 'bin')

	path_info = _create_path_info_from_sdk(sdk)
	lib_path = path.normpath(path.join(build.orig_wd, '.template', 'lib'))
	dev_dir = path.normpath(path.join(build.orig_wd, 'development', 'android'))

	LOG.info('Starting ADB if not running')
	run_detached([path_info.adb, 'start-server'], wait=True)

	LOG.info('Looking for Android device')
	
	available_devices = _get_available_devices(path_info)

	if not available_devices:
		# TODO: allow for prompting of user in either webui situation or commandline situation
		if interactive:
			_prompt_user_to_attach_device(path_info)
		else:
			_create_avd_if_necessary(path_info)
			_launch_avd(path_info)

		return run_android(build, build_type_dir, sdk, device, interactive=interactive)

	if device:
		if device in available_devices:
			chosen_device = device
			LOG.info('Using specified android device %s' % chosen_device)
		else:
			LOG.error('No such device "%s"' % device)
			LOG.error('The available devices are:')
			LOG.error("\n".join(available_devices))
			raise AndroidError
	else:
		chosen_device = available_devices[0]
		LOG.info('No android device specified, defaulting to %s' % chosen_device)
	package_name = _generate_package_name(build)
	
	LOG.info('Creating Android .apk file')

	with temp_file() as zipf_name:
		# Compile XML files into APK
		_create_apk_with_aapt(zipf_name, path_info, package_name, lib_path, dev_dir)
		
		with temp_file() as signed_zipf_name:
			# Sign APK
			_sign_zipf_debug(lib_path, jre, zipf_name, signed_zipf_name)
			
			with temp_file() as out_apk_name:
				# Align APK
				_align_apk(sdk, signed_zipf_name, out_apk_name)

				package_name = _generate_package_name(build)
				
				# If required remove previous installs from device
				if purge:
					_run_adb([path_info.adb, 'uninstall', package_name], 30, path_info)

				# Install APK to device
				LOG.info('Installing apk')
				proc_std = _run_adb([path_info.adb, '-s', chosen_device, 'install', '-r', out_apk_name], 60, path_info)
				LOG.debug(proc_std)

				# Start app on device
				proc_std = _run_adb([path_info.adb, '-s', chosen_device, 'shell', 'am', 'start', '-n', package_name+'/io.trigger.forge.android.template.LoadActivity'], 60, path_info)
				LOG.debug(proc_std)
				
				#follow log
				_follow_log(path_info, chosen_device)

def _create_output_directory(output):
	'output might be in some other directory which does not yet exist'
	directory = path.dirname(output)
	if not path.isdir(directory):
		os.makedirs(directory)

@task
def package_android(build):
	sdk = build.tool_config.get('android.sdk')
	interactive = build.tool_config.get('general.interactive', True)
	if sdk:
		sdk = path.normpath(path.join(build.orig_wd, sdk))
	sdk = _check_for_sdk(sdk, interactive=interactive)
	keystore = build.tool_config.get('android.profile.keystore')
	storepass = build.tool_config.get('android.profile.storepass')
	keyalias = build.tool_config.get('android.profile.keyalias')
	keypass = build.tool_config.get('android.profile.keypass')

	path_info = _create_path_info_from_sdk(sdk)
	lib_path = path.normpath(path.join(build.orig_wd, '.template', 'lib'))
	dev_dir = path.normpath(path.join(build.orig_wd, 'development', 'android'))

	SigningInfoPrompt = namedtuple('SigningInfoPrompt', 'name description secure')
	signing_info = {}
	file_name = "{name}-{time}.apk".format(
		name=re.sub("[^a-zA-Z0-9]", "", build.config["name"].lower()),
		time=str(int(time.time()))
	)
	output = path.abspath(path.join('release', 'android', file_name))
	
	if not interactive and not all((keystore, storepass, keyalias, keypass)):
		raise AndroidError("When running in non-interactive mode, keystore, storepass, keyalias and keypass arguments must be supplied")
	
	signing_info["keystore"] = keystore
	signing_info["storepass"] = storepass
	signing_info["keyalias"] = keyalias
	signing_info["keypass"] = keypass
	
	if interactive:
		signing_prompts = (
			SigningInfoPrompt(name="keystore", description="the location of your release keystore", secure=False),
			SigningInfoPrompt(name="storepass", description="the password of your release keystore", secure=True),
			SigningInfoPrompt(name="keyalias", description="the alias of your release key", secure=False),
			SigningInfoPrompt(name="keypass", description="the password for your release key", secure=True),
		)
		for prompt in signing_prompts:
			if signing_info[prompt.name]:
				# value given supplied in configuration or on command line
				continue
				
			response = ""
			while not response:
				msg = "Please enter {0}: ".format(prompt.description)
				if prompt.secure:
					response = getpass(msg)
				else:
					response = raw_input(msg)
			signing_info[prompt.name] = response
	
	# need to make fix paths which may be relative to original build directory
	if sdk:
		sdk = path.normpath(path.join(build.orig_wd, sdk))
	signing_info["keystore"] = path.normpath(path.join(build.orig_wd, signing_info["keystore"]))
	
	sdk = _check_for_sdk(sdk, interactive=interactive)
	jre = ""

	if not check_for_java():
		jres = _look_for_java()
		if not jres:
			raise AndroidError("Java not found: Java must be installed and available in your path in order to create Android packages.")
		jre = path.join(jres[0], 'bin')

	LOG.info('Creating Android .apk file')
	package_name = _generate_package_name(build)
	#zip
	with temp_file() as zipf_name:
		_create_apk_with_aapt(zipf_name, path_info, package_name, lib_path, dev_dir)

		with temp_file() as signed_zipf_name:
			#sign
			_sign_zipf_release(lib_path, jre, zipf_name, signed_zipf_name, **signing_info)

			# create output directory for APK if necessary
			_create_output_directory(output)

			#align
			_align_apk(sdk, signed_zipf_name, output)
			LOG.debug('removing zipfile and un-aligned APK')

			LOG.info("created APK: {output}".format(output=output))
			return output
