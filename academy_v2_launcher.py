"""Development entrypoint for the Academy v2 mobile learning route.

Production start command remains unchanged. This launcher installs the new Academy
navigation first, then loads the existing telemetry/launcher stack.
"""
from academy.mobile_learning_runtime import install

install()

import telemetry_launcher  # noqa: E402


if __name__ == "__main__":
    telemetry_launcher.launcher.bot.main()
