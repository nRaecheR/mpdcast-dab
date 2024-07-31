# Copyright (C) 2024 Lamarqe
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License
# as published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Python interface defintions for welle.io python wrapper"""

import asyncio
import atexit
import time
import logging
import mpdcast_dab.welle_python.libwelle_py as welle_io

logger = logging.getLogger(__name__)


class ProgrammeHandlerInterface():

  async def on_frame_errors(self, frame_errors: int) -> None:
    pass

  async def on_new_audio(self, audio_data: bytes, sample_rate: int, mode: str) -> None:
    pass

  async def on_rs_errors(self, uncorrected_errors: int, num_corrected_errors: int) -> None:
    pass

  async def on_aac_errors(self, aac_errors: int) -> None:
    pass

  async def on_new_dynamic_label(self, label: str) -> None:
    pass

  async def on_mot(self, data: bytes, mime_type: str, name: str) -> None:
    pass


class RadioControllerInterface():

  async def on_snr(self, snr: float) -> None:
    pass

  async def on_frequency_corrector_change(self, fine: int, coarse: int) -> None:
    pass

  async def on_sync_change(self, is_sync: int) -> None:
    pass

  async def on_signal_presence(self, is_signal: int) -> None:
    pass

  async def on_service_detected(self, service_id: int) -> None:
    pass

  async def on_new_ensemble(self, ensemble_id: int) -> None:
    pass

  async def on_set_ensemble_label(self, label: str) -> None:
    pass

  async def on_datetime_update(self, timestamp: int) -> None:
    pass

  async def on_fib_decode_success(self, crc_check_ok: int, fib: int) -> None:
    pass

  async def on_message(self, text: str, text2: str, is_error: int) -> None:
    pass


class CallbackForwarder():
  # This class forwards all c-lib callbacks to the actual RadioControllerInterface
  # Making this indirect has two reasons:
	# 1: It allows dynamic controller objects in python without having to re-initialize the device in C
  # 2: The indirection includes a thread handover into async which is required anyways

  def __init__(self, target = None):
    self._forward_object = target
    self._loop = asyncio.get_event_loop()

  def subscribe_for_callbacks(self, target) -> bool:
    if self._forward_object is not None:
      return False
    self._forward_object = target
    return True

  def unsubscribe_from_callbacks(self) -> bool:
    if self._forward_object is None:
      return False
    self._forward_object = None
    return True

  def __getattr__(self, attr):
    method = getattr(self._forward_object, attr)
    def asyncio_callback(*args, **kwargs):
      asyncio.run_coroutine_threadsafe(method(*args, **kwargs), self._loop)
    return asyncio_callback

class DabDevice():
  def __init__(self, device_name: str = 'auto', gain: int = -1):
    self._forwarder = CallbackForwarder()
    self._capsule = welle_io.init_device(self._forwarder, device_name, gain)
    if self._capsule:
      atexit.register(self.cleanup)

  def aquire_now(self, radio_controller: RadioControllerInterface) -> bool:
    return self._forwarder.subscribe_for_callbacks(radio_controller)

  def release(self) -> bool:
    return self._forwarder.unsubscribe_from_callbacks()

  def is_usable(self) -> bool:
    return self._capsule is not None

  def set_channel(self, channel: str, is_scan: bool = False) -> bool:
    if not self._capsule:
      return False
    return welle_io.set_channel(self._capsule, channel, is_scan)

  def subscribe_program(self, handler: ProgrammeHandlerInterface, service_id: int) -> bool:
    if not self._capsule:
      return False
    forwarder = CallbackForwarder(handler)
    handler._program_forwarder = forwarder
    return welle_io.subscribe_program(self._capsule, forwarder, service_id)

  def unsubscribe_program(self, service_id: int) -> bool:
    if not self._capsule:
      return False
    return welle_io.unsubscribe_program(self._capsule, service_id)

  def cleanup(self) -> None:
    if self._capsule:
      welle_io.set_channel(self._capsule, '', False)
      welle_io.close_device(self._capsule)
      # wait for all c-lib callbacks to be processed in python. Otherwise we might deadlock
      time.sleep(0.1)
      welle_io.finalize(self._capsule)
      self._capsule = None

  def get_service_name(self, service_id: int) -> str:
    if not self._capsule:
      return None
    return welle_io.get_service_name(self._capsule, service_id)

  def is_audio_service(self, service_id: int) -> bool:
    if not self._capsule:
      return None
    return welle_io.is_audio_service(self._capsule, service_id)

  @staticmethod
  def all_channel_names() -> list[str]:
    return welle_io.all_channel_names()
