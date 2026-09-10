from atelier2.host import main

# Only when run: a spawned child imports whichever module started this process
# so that the names it was handed resolve, and an unguarded call would run the
# whole command again in every such child.
if __name__ == "__main__":
    raise SystemExit(main())
