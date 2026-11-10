import sys

from .launch import prepare

prepare()

from .cli import main  # noqa: E402

sys.exit(main())
