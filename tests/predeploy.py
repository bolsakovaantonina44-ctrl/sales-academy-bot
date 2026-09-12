import subprocess
import sys


def run(*args):
    print('PREDEPLOY_RUN', ' '.join(args), flush=True)
    subprocess.check_call([sys.executable, *args])


if __name__ == '__main__':
    run('-m', 'unittest', 'discover', '-s', 'tests')
    run('-u', 'tests/smoke_mvp.py')
    print('PREDEPLOY_PASS', flush=True)
