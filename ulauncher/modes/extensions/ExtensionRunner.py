from __future__ import annotations

import json
import logging
import signal
import sys
from collections import deque
from functools import lru_cache, partial
from time import time
from typing import NamedTuple

if sys.version_info >= (3, 8):
    from typing import Literal

    ExtensionRuntimeError = Literal["Terminated", "Exited", "MissingModule", "Incompatible", "Invalid"]
else:
    ExtensionRuntimeError = str

from gi.repository import Gio, GLib

from ulauncher.config import PATHS, get_options
from ulauncher.modes.extensions.ExtensionDb import ExtensionDb
from ulauncher.modes.extensions.ExtensionManifest import (
    ExtensionIncompatibleWarning,
    ExtensionManifest,
    ExtensionManifestError,
)
from ulauncher.utils.timer import timer

logger = logging.getLogger()
ext_db = ExtensionDb.load()


class ExtensionProc(NamedTuple):
    ext_id: str
    subprocess: Gio.Subprocess
    start_time: float
    error_stream: Gio.DataInputStream
    recent_errors: deque


class ExtensionRunner:
    @classmethod
    @lru_cache(maxsize=None)
    def get_instance(cls) -> ExtensionRunner:
        return cls()

    def __init__(self) -> None:
        self.extension_procs: dict[str, ExtensionProc] = {}
        self.verbose = get_options().verbose

    def run(self, ext_id: str, ext_path: str) -> None:
        """
        * Validates manifest
        * Runs extension in a new process
        """
        if not self.is_running(ext_id):
            ext_record = ext_db.get_record(ext_id)
            ext_record.update(error_message="", error_type="")  # reset

            manifest = ExtensionManifest.load(ext_path)

            try:
                manifest.validate()
                manifest.check_compatibility(verbose=True)
            except ExtensionManifestError as err:
                self.set_extension_error(ext_id, "Invalid", str(err))
                return
            except ExtensionIncompatibleWarning as err:
                self.set_extension_error(ext_id, "Incompatible", str(err))
                return

            triggers = {id: t.keyword for id, t in manifest.triggers.items() if t.keyword}
            # Preferences used to also contain keywords, so adding them back to avoid breaking v2 code
            backwards_compatible_preferences = {**triggers, **manifest.get_key_value_user_preferences(ext_id)}
            cmd = [sys.executable, f"{ext_path}/main.py"]
            env = {
                "VERBOSE": str(int(self.verbose)),
                "PYTHONPATH": PATHS.APPLICATION,
                "EXTENSION_PREFERENCES": json.dumps(backwards_compatible_preferences, separators=(",", ":")),
            }

            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDERR_PIPE)
            for env_name, env_value in env.items():
                launcher.setenv(env_name, env_value, True)

            t_start = time()
            subproc = launcher.spawnv(cmd)
            error_input_stream = subproc.get_stderr_pipe()
            if not error_input_stream:
                err_msg = "Subprocess must be created with Gio.SubprocessFlags.STDERR_PIPE"
                raise AssertionError(err_msg)
            error_line_str = Gio.DataInputStream.new(error_input_stream)
            self.extension_procs[ext_id] = ExtensionProc(
                ext_id=ext_id,
                subprocess=subproc,
                start_time=t_start,
                error_stream=error_line_str,
                recent_errors=deque(maxlen=1),
            )
            logger.debug("Launched %s using Gio.Subprocess", ext_id)

            subproc.wait_async(None, self.handle_exit, ext_id)
            self.read_stderr_line(self.extension_procs[ext_id])

    def read_stderr_line(self, proc: ExtensionProc) -> None:
        proc.error_stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.handle_stderr, proc.ext_id)

    def handle_stderr(self, error_stream: Gio.DataInputStream, result: Gio.AsyncResult, ext_id: str) -> None:
        output, _ = error_stream.read_line_finish_utf8(result)
        if output:
            print(output)  # noqa: T201
        proc = self.extension_procs.get(ext_id)
        if not proc:
            logger.debug("Extension process context for %s no longer present", ext_id)
            return
        if output:
            proc.recent_errors.append(output)
        self.read_stderr_line(proc)

    def handle_exit(self, subprocess: Gio.Subprocess, _result: Gio.AsyncResult, ext_id: str) -> None:
        if subprocess.get_if_signaled() and self.extension_procs.get(ext_id):
            kill_signal = subprocess.get_term_sig()
            error_msg = f'Extension "{ext_id}" was terminated with signal {kill_signal}'
            logger.error(error_msg)
            self.set_extension_error(ext_id, "Terminated", error_msg)
            self.extension_procs.pop(ext_id, None)
            return

        proc = self.extension_procs.get(ext_id)
        if not proc or id(proc.subprocess) != id(subprocess):
            logger.info("Exited process %s for %s has already been removed.", subprocess, ext_id)
            return

        uptime_seconds = time() - proc.start_time
        code = subprocess.get_exit_status()
        if uptime_seconds < 1:
            default_error_msg = f'Extension "{ext_id}" exited instantly with code {code}'
            lasterr = "\n".join(proc.recent_errors)
            logger.error('Extension "%s" failed with an error: %s', ext_id, lasterr)
            if "ModuleNotFoundError" in lasterr:
                package_name = lasterr.split("'")[1]
                if package_name == "ulauncher":
                    logger.error(
                        "Extension tried to import Ulauncher modules which have been moved or removed. "
                        "This is likely Ulauncher internals which were not part of the extension API. "
                        "Extensions importing these can break at any Ulauncher release."
                    )
                    self.set_extension_error(ext_id, "Incompatible", default_error_msg)
                elif package_name:
                    self.set_extension_error(ext_id, "MissingModule", package_name)
            else:
                self.set_extension_error(ext_id, "Terminated", default_error_msg)

            self.extension_procs.pop(ext_id, None)
            return

        error_msg = f'Extension "{ext_id}" exited with code {code} after {uptime_seconds} seconds.'
        self.set_extension_error(ext_id, "Exited", error_msg)
        logger.error(error_msg)
        self.extension_procs.pop(ext_id, None)

    def stop(self, ext_id: str) -> None:
        """
        Terminates extension
        """
        if self.is_running(ext_id):
            logger.info('Terminating extension "%s"', ext_id)
            proc = self.extension_procs[ext_id]
            self.extension_procs.pop(ext_id, None)

            proc.subprocess.send_signal(signal.SIGTERM)

            timer(0.5, partial(self.confirm_termination, proc))

    def confirm_termination(self, proc: ExtensionProc) -> None:
        if proc.subprocess.get_identifier():
            logger.info("Extension %s still running, sending SIGKILL", proc.ext_id)
            # It is possible that the process exited between the check above and this signal,
            # luckily the subprocess library handles the signal delivery in race-free way, so this
            # is safe to do.
            proc.subprocess.send_signal(signal.SIGKILL)

    def is_running(self, ext_id: str) -> bool:
        return ext_id in self.extension_procs

    def set_extension_error(self, ext_id: str, error_type: ExtensionRuntimeError, message: str) -> None:
        ext_db.get_record(ext_id).update(error_message=message, error_type=error_type)
        ext_db.save()
