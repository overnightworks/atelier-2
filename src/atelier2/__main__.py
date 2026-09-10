from atelier2.host import main

# Only when run: running the command is an act, never a side effect of some
# other process importing this module by name.
if __name__ == "__main__":
    raise SystemExit(main())
