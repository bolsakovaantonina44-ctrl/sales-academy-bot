"""Optional child process inside the EXISTING Railway service."""
import logging
import os
import subprocess
import sys
import threading


def start():
    if os.getenv('CENTARO_ENABLED', '').lower() not in ('true', '1'):
        return lambda: None
    if not os.getenv('CENTARO_TELEGRAM_TOKEN'):
        raise RuntimeError('CENTARO_ENABLED requires CENTARO_TELEGRAM_TOKEN')
    stopped = threading.Event()
    child = []

    def supervise():
        while not stopped.is_set():
            process = subprocess.Popen([sys.executable, '-m', 'centaro.runtime'])
            child.append(process)
            if stopped.is_set():
                process.terminate()
            process.wait()
            child.remove(process)
            if not stopped.is_set():
                logging.error('Centaro process exited; retry in 60 seconds')
                stopped.wait(60)

    thread = threading.Thread(target=supervise, daemon=True)
    thread.start()

    def close():
        stopped.set()
        for process in list(child):
            process.terminate()
        thread.join(5)
    return close
