"""Reading processes this suite starts in place of the production one.

`supervised_reading` is told which module a reading process runs, so each
module here is one whole reading process that goes wrong in exactly one way:
it dies where a real one would read, refuses to be stopped, breaks while
putting its client away, or writes onto the pipe something no record could be.
Each runs as much of the production reader as its own defect leaves standing,
and whatever it replaces it replaces in its own process -- nothing here can
reach the process that reports.
"""
