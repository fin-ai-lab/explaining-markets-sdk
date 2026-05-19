"""`python -m demo` shim — delegates to `demo.test:main`."""

from .test import main

if __name__ == "__main__":
    main()
