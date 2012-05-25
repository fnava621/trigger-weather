import socket
import time
import errno
import json
import logging
import os
from os import path
import re
import shutil
import sys
import subprocess
import threading
from getpass import getpass
from urlparse import urljoin

import requests

import lib
from lib import cd, task
from utils import run_shell, ShellError

LOG = logging.getLogger(__name__)

class WebError(lib.BASE_EXCEPTION):
	pass

def _set_dotted_attribute(path_to_app, attribute_name, value):
	from forge import build_config
	LOG.info('Saving %s as %s in local_config.json' % (value, attribute_name))
	with cd(path_to_app):
		local_config = build_config.load_local()
		current_level = local_config
		crumbs = attribute_name.split('.')
		for k in crumbs[:-1]:
			if k not in current_level:
				current_level[k] = {}
			current_level = current_level[k]
		current_level[crumbs[-1]] = value

		build_config.save_local(local_config)

def _port_available(port):
	s = None
	try:
		# need to set SO_REUSEADDR, otherwise we get false positives where the port is
		# unavailable for a small period of time after closing node

		# http://stackoverflow.com/questions/6380057/python-binding-socket-address-already-in-use
		# http://stackoverflow.com/questions/775638/using-so-reuseaddr-what-happens-to-previously-open-socket
		s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		s.bind(('127.0.0.1', port))
		return True

	except socket.error:
		return False

	finally:
		if s is not None:
			s.close()

def _npm(*args, **kw):
	if sys.platform.startswith("win"):
		npm = "npm.cmd"
	else:
		npm = "npm"

	try:
		run_shell(npm, *args, **kw)
	except OSError as e:
		if e.errno == errno.ENOENT:
			raise WebError("failed to run npm: do you have Node.js installed and on your path?")

@task
def clean_web(build):
	# TODO: port should be a parameter/configuration
	port = 3000
	requests.post('http://localhost:%d/_forge/kill/' % port)
	time.sleep(1)

@task
def run_web(build):
	# run Node locally
	# TODO: port should be a parameter/configuration
	port = 3000

	def show_local_server():
		LOG.info("Attempting to open browser at http://localhost:%d/" % port)
		_open_url("http://localhost:%d/" % port)

	with cd(path.join("development", "web")):
		timer = None
		try:
			_npm("install")

			attempts = 0
			while not _port_available(port):
				LOG.info('Port still in use, attempting to send a kill signal')
				#TODO: appropriate timeout and handling
				requests.post('http://localhost:%d/_forge/kill/' % port)

				time.sleep(1)

				attempts += 1
				if attempts > 5:
					raise WebError("Port %d seems to be in use, you should specify a different port to use" % port)

			timer = threading.Timer(3, show_local_server).start()
			_npm("start", command_log_level=logging.INFO, env=dict(os.environ, PORT=str(port), FORGE_DEBUG='1'))

		finally:
			if timer:
				timer.cancel()

def _git(cmd, *args, **kwargs):
	"""Runs a git command and scrapes the output for common problems, so that we can try
	to advise the user about them

	e.g. _git('push', '--all')
	"""
	try:
		output = run_shell('git', cmd, *args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
	except OSError as e:
		if e.errno == errno.ENOENT:
			# TODO: download portable copy of git/locate git?
			raise WebError("Can't run git commands - you need to install git and make sure it's in your PATH")
	except ShellError as e:
		def _key_problem(output):
			lines = output.split('\n')
			if len(lines) > 0:
				first = lines[0]
				return first.startswith('Permission denied (publickey)')

		if _key_problem(e.output):
			# TODO: prompt user with choice to use existing .pub in ~/.ssh
			# or create new keypair and submit to heroku
			raise WebError('Failed to access remote git repo, you need to set up key based access')

		raise WebError('Problem running git {cmd}:\n {output}'.format(cmd=cmd, output=e.output))

	return output

def _request_heroku_credentials():
	"""Prompts the user for heroku username and password and returns them"""
	heroku_username = raw_input('Your heroku username: ')
	heroku_password = getpass('Password: ')
	return heroku_username, heroku_password

def _present_choice(message, choices, prompt):
	"""Presents the user with a numerical choice on the command line
	
	:param message: The question to ask the user
	:param choices: A list of possible choices
	:param prompt: The text shown to the user on the line they enter their choice
	:return n: An int, in range(len(choices))

	*NB* asks repeatedly until the user enters a valid choice
	"""
	lines = ["%d) %s" % (i,choices[i]) for i in xrange(len(choices))]
	
	LOG.info(
		message + "\n" + "\n".join(lines)
	)

	choice = None
	while choice is None:
		try:
			inp = raw_input(prompt)
			n = int(inp.strip())

			if not (0 <= n < len(choices)):
				raise ValueError

			choice = n
		except ValueError:
			LOG.info("Invalid choice")

	return choice

def _check_heroku_response(response):
	if not response.ok:
		msg = (
			"Request to Heroku API went wrong\n"
			"url: {url}\n"
			"code: {code}"
		).format(url=response.request.url, code=response.status_code)

		if response.status_code == 401:
			msg += "\n\nUnauthorized - make sure the value of web.profile.heroku_api_key is correct"

		raise WebError(msg)

def _heroku_get_api_key(username, password):
	response = requests.post(
		'https://api.heroku.com/login', 
		data={
			'username': username, 
			'password': password
		}, 
		headers={
			'Accept': 'application/json'
		}
	)

	try:
		_check_heroku_response(response)
	except WebError:
		LOG.error("Incorrect heroku details")
		return None

	#The json you get back:
	#'{"verified_at":null,"api_key":"yourapikey","id":"839828@users.heroku.com","verified":false,"email":"t.r.monks@gmail.com"}'
	return json.loads(response.content)['api_key']

# TODO: error code checking on responses
def _heroku_get(api_key, api_url):
	# see https://api-docs.heroku.com/apps
	# heroku api requires a blank user and api_key as http auth details
	auth = ('', api_key)
	headers = {
		'Accept': 'application/json',
	}
	url = urljoin('https://api.heroku.com/', api_url)
	response = requests.get(url, auth=auth, headers=headers)
	_check_heroku_response(response)
	return response

# TODO: error code checking on responses
def _heroku_post(api_key, api_url, data):
	# heroku api requires a blank user and api_key as http auth details
	auth = ('', api_key)
	headers = {
		'Accept': 'application/json',
	}
	url = urljoin('https://api.heroku.com/', api_url)
	response = requests.post(url, data=data, auth=auth, headers=headers)
	_check_heroku_response(response)
	return response

def _open_url(url):
	'''Attempt to open the provided URL in the default browser'''
	if sys.platform.startswith('darwin'):
		run_shell('open', url, fail_silently=True)
	elif sys.platform.startswith('win'):
		# 'start' seems to need shell=True to be found (probably a builtin)
		cmd = subprocess.list2cmdline(['start', url])
		subprocess.call(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
	elif sys.platform.startswith('linux'):
		run_shell('xdg-open', url, fail_silently=True)

def _request_app_to_push_to(build, api_key, interactive):
	if not interactive:
		app = build.tool_config.get('web.profile.heroku_app_name')
		if app is None:
			raise WebError("You need to specify the name of a heroku application to push to in your settings")
		return app

	LOG.info('Querying heroku about registered apps...')
	apps = json.loads(_heroku_get(api_key, 'apps').content)
	app_names = [app['name'] for app in apps]

	create_new_heroku_app = True
	if app_names:
		message = (
			"You don't have a heroku app name specified in local_config.json."
			'You can choose either to:'
		)

		chosen_n = _present_choice(message, ['Create a new heroku application', 'Push to a currently registered heroku application'], 'Choice: ')

		if chosen_n == 0:
			create_new_heroku_app = True
		else:
			create_new_heroku_app = False

	# either create a new heroku app, or choose an already existing app
	if create_new_heroku_app:
		# TODO: allow user to specify app name?
		# have to deal with name already taken
		LOG.info('Creating new heroku application')
		response = _heroku_post(api_key, 'apps', data='app[stack]=cedar')
		chosen_app = json.loads(response.content)['name']

	else:
		chosen_n = _present_choice('Choose an existing heroku app to deploy to:', 
				app_names, 'Deploy to: ')

		chosen_app = app_names[chosen_n]

	return chosen_app

@task
def package_web(build):
	path_to_app = path.abspath('')
	interactive = build.tool_config.get('general.interactive', True)
	development = path.abspath(path.join('development', 'web'))
	output = path.abspath(path.join('release', 'web', 'heroku'))

	# deploy to Heroku
	with cd(development):
		api_key = build.tool_config.get('web.profile.heroku_api_key')

		while api_key is None:
			if not interactive:
				raise WebError("You need to specify an API Key for interaction with heroku")

			# TODO: may want to check the api key is actually valid by hitting the api?
			heroku_username, heroku_password = _request_heroku_credentials()
			api_key = _heroku_get_api_key(heroku_username, heroku_password)

			if api_key is not None:
				_set_dotted_attribute(
					path_to_app,
					'web.profiles.%s.heroku_api_key' % build.tool_config.profile(),
					api_key
				)

		chosen_app = build.tool_config.get('web.profile.heroku_app_name')

		if not path.isdir(output):
			os.makedirs(output)

		with cd(output):
			if not path.isdir('.git'):
				LOG.debug('Creating git repo')
				_git('init')

				LOG.debug('Create dummy first commit')
				with open('.forge.txt', 'w') as forge_file:
					forge_file.write('')
				_git('add', '.')
				_git('commit', '-am', '"first commit"')

			if chosen_app is None:
				chosen_app = _request_app_to_push_to(build, api_key, interactive)

				_set_dotted_attribute(
					path_to_app,
					'web.profiles.%s.heroku_app_name' % build.tool_config.profile(),
					chosen_app
				)

		# remove all previous files/folders except for .git!
		with cd(output):
			for f in os.listdir('.'):
				if not f == '.git':
					if path.isfile(f):
						os.remove(f)

					elif path.isdir(f):
						shutil.rmtree(f)

		# copy code from development to release!
		with cd(development):
			for f in os.listdir('.'):
				if path.isfile(f):
					shutil.copy2(f, output)
				elif path.isdir(f) and path.basename(f) != '.git':
					shutil.copytree(f, path.join(output, f), ignore=shutil.ignore_patterns('.git'))

		with cd(output):
			# setup with the specified remote
			LOG.debug('Setting up git remote for %s' % chosen_app)

			# remove any previous remote
			try:
				_git('remote', 'rm', 'heroku')
			except WebError:
				pass

			_git('remote', 'add', 'heroku', 'git@heroku.com:%s.git' % chosen_app)

			# commit
			_git('add', '.')
			diff = _git('diff', 'HEAD')
			if not diff.strip():
				if interactive:
					LOG.warning("No app changes detected: did you forget to forge build?")
				else:
					# not interactive basically means we're using the trigger toolkit, where 'forge build'
					# doesn't really make sense
					LOG.warning("No app changes detected, pushing to heroku anyway")
			else:
				_git('commit', '-am', 'forge package web')

			# push
			LOG.info('Deploying to %s.herokuapp.com' % chosen_app)

			if not interactive:
				LOG.warning('You may need to check the commandline to enter an SSH key passphrase')

			push_output = _git('push', 'heroku', '--all', '--force', command_log_level=logging.INFO)

			if push_output.startswith('Everything up-to-date'):
				remote_output = _git('remote', '-v')
				remote_pattern = re.compile(r'git@heroku.com:(.*?).git \(fetch\)')

				remote_match = remote_pattern.search(remote_output)
				if remote_match:
					app_url = 'http://%s.herokuapp.com' % remote_match.group(1)
					_open_url(app_url)
					LOG.info('Deployed at %s' % app_url)

			else:
				deploy_pattern = re.compile(r'(http://[^ ]+) deployed to Heroku')
				deploy_match = deploy_pattern.search(push_output)
				if deploy_match:
					_open_url(deploy_match.group(1))
					LOG.info('Deployed at %s' % deploy_match.group(1))
