"""Entry point.

python -m triage            run the service (API, worker)
python -m triage serve      the same
python -m triage eval ...   run the eval suite (see triage.evals)
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "serve"
    if cmd == "serve":
        import uvicorn

        from . import logs, tracing
        from .api import create_app
        from .config import Settings

        s = Settings.from_env()
        logs.setup(s.log_level)
        tracing.setup()
        uvicorn.run(create_app(s), host=s.http_host, port=s.http_port, log_config=None, access_log=False)
    elif cmd == "eval":
        from .evals.run import main as eval_main

        sys.exit(eval_main(args[1:]))
    else:
        sys.exit(f"unknown command {cmd!r}: want serve or eval")


if __name__ == "__main__":
    main()
