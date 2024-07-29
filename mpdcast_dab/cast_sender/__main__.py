#!/usr/bin/env python3
# coding=utf-8

import io
import os
import sys
import asyncio
import argparse
import socket
import ifaddr
import time
import logging
from aiohttp import web

import threading
import traceback

if __name__ == '__main__':
  sys.path.append(os.path.dirname(__file__)  + '/../..')

from mpdcast_dab.cast_sender.output_grabber import *
import mpdcast_dab.cast_sender.imageserver as imageserver
from mpdcast_dab.cast_sender.mpd_caster import *

from mpdcast_dab.welle_python.dab_server import DabServer

def get_first_ipv4_address():
  for iface in ifaddr.get_adapters():
    for addr in iface.ips:
      # Filter out link-local addresses.
      if addr.is_IPv4:
        if not (addr.ip.startswith('169.254.') or addr.ip == '127.0.0.1'):
          return addr.ip
  return None

def load_mpd_config(config_filename):
  logger.info('Loading config from %s', config_filename)
  cfg_file = open(config_filename, "r")
  confStr = cfg_file.read()

  # convert curly brace groups to toml arrays
  confStr = re.sub(r"\n([^\s#]*?)\s*{(.*?)}", r"\n[[\1]]\2\n", confStr, flags=re.S, count=0)
  # separate key and value with equals sign
  confStr = re.sub(r"^\s*(\w+)\s*(.*)$", r"\1 = \2", confStr, flags=re.M, count=0)
  # now the config should adhere to toml spec.
  mpd_config = tomllib.loads(confStr)
  cfg_file.close()

  return mpd_config

def read_mpd_config(config):
  mpd_port = int(config.get("port", "6600"))

  httpd_defined  = False
  streaming_port = None
  device_name    = None
  if "audio_output" in config:
    for audio_output in config["audio_output"]:
      if 'type' in audio_output and audio_output['type'] == 'httpd':
        httpd_defined = True
        if 'port' in audio_output:
          streaming_port = audio_output['port']
        if 'name' in audio_output:
          device_name = audio_output['name']

  if not httpd_defined:
    raise SyntaxError('No httpd audio output defined.')
  if not streaming_port:
    raise SyntaxError('No httpd streaming port defined.')
  if not streaming_port.isdigit():
    raise SyntaxError('Invalid http streaming port defined: ' + streaming_port + '.')
  if not device_name:
    raise SyntaxError('No cast device name defined')

  return mpd_port, device_name, streaming_port

async def setup_webserver(runner, port):
  await runner.setup()
  site = web.TCPSite(runner, '0.0.0.0', port, shutdown_timeout=0.1)
  await site.start()


def updateLoggerConfig(verbose):
  internal_log_level = logging.INFO    if verbose else logging.WARNING
  external_log_level = logging.WARNING if verbose else logging.ERROR
  logging.basicConfig(format='%(name)s - %(levelname)s: %(message)s', encoding='utf-8', level=internal_log_level, stream=sys.stdout, force=True)
  logging.getLogger("aiohttp").setLevel(external_log_level)
  logging.getLogger("pychromecast").setLevel(external_log_level)
  logging.getLogger("zeroconf").setLevel(external_log_level)
  logging.getLogger("Welle.io").setLevel(external_log_level)

async def get_webui(request):
  return web.FileResponse('/usr/share/mpdcast-dab/webui/index.htm')

def main():
  CAST_PATH = '/cast_receiver'
  CAST_PAGE = 'receiver.html'
  WEB_PORT = 8080

  parser = argparse.ArgumentParser(description='MPD Cast Device Agent')
  parser.add_argument('--verbose', help = 'Enable verbose output', action = 'store_true')
  parser.add_argument('--conf', help = 'mpd config file to use. Default: /etc/mpd.conf', default = '/etc/mpd.conf')
  args = vars(parser.parse_args())

  stdout_grabber = OutputGrabber(sys.stdout, 'Welle.io', logging.Logger.error)
  stderr_grabber = OutputGrabber(sys.stderr, 'Welle.io', logging.Logger.warning)
  sys.stdout = stdout_grabber.redirect_stream()
  sys.stderr = stderr_grabber.redirect_stream()
  updateLoggerConfig(args['verbose'])
  
  # Initialize some status vars
  init_mpdcast_ok = True
  init_dab_ok     = True

  my_ip = get_first_ipv4_address()
  if not my_ip:
    logger.warning('Could not retrieve local IP address')
    init_mpdcast_ok = False
    # Set up fallback that can be used for DAB playlist creation
    my_ip = '127.0.0.1'

  cast_receiver_url = 'http://' + my_ip + ':' + str(WEB_PORT) + CAST_PATH + '/' + CAST_PAGE
  try:
    mpd_config = load_mpd_config(args['conf'])
    mpd_port, device_name, streaming_port = read_mpd_config(mpd_config)
  except (FileNotFoundError, SyntaxError) as error:
    logger.warning('Failed to read MPD Cast configuration. Disabling.')
    logger.warning(str(error))
    init_mpdcast_ok = False

  dab_server = DabServer(my_ip, WEB_PORT)
  if dab_server.radio_controller is None:
    logger.warning('No DAB device available')
    init_dab_ok = False

  if not init_mpdcast_ok and not init_dab_ok:
    logger.error('Fatal. Both MpdCast and DAB processing failed to initialize. Exiting.')
    stdout_grabber.cleanup()
    stderr_grabber.cleanup()
    sys.exit(1)

  web_app = web.Application()

  if init_mpdcast_ok:
    web_app.add_routes([web.get(r'', get_webui), web.static(CAST_PATH, '/usr/share/mpdcast-dab/cast_receiver')])
    image_request_handler = imageserver.ImageRequestHandler(my_ip, WEB_PORT)
    web_app.add_routes(image_request_handler.get_routes())

  if init_dab_ok:
    web_app.add_routes(dab_server.get_routes())

  runner = web.AppRunner(web_app)
  try:
    loop = asyncio.get_event_loop()
    loop.run_until_complete(setup_webserver(runner, WEB_PORT))

    # run the webserver in parallel to the cast task
    while True:
      if init_mpdcast_ok:
        # wait until we find the cast device in the network
        mpd_caster = MpdCaster(my_ip, cast_receiver_url, mpd_port, device_name, streaming_port, image_request_handler)
        mpd_caster.waitfor_and_register_device()
        # run the cast (until chromecast disconnects)
        loop.run_until_complete(mpd_caster.cast_forever())
      else:
        # DAB processing is fully built into the web server. no additional tasks required
        loop.run_until_complete(asyncio.sleep(3600))

  except KeyboardInterrupt:
    if init_mpdcast_ok:
      loop.run_until_complete(mpd_caster.stop())
    if init_dab_ok:
      loop.run_until_complete(dab_server.stop())
    loop.run_until_complete(runner.cleanup())
    stdout_grabber.cleanup()
    stderr_grabber.cleanup()

if __name__ == '__main__':
  main()
