workers = 1
worker_class = "gthread"
threads = 4
timeout = 60

def post_fork(server, worker):
    # Gunicorn's master/arbiter process also imports the app to validate
    # the WSGI callable, which would otherwise run the background fetch
    # scheduler there too -- a separate process whose state is invisible
    # to the worker that actually serves requests. Starting it here means
    # it only ever runs inside the worker process handling traffic.
    from app import start_scheduler
    start_scheduler()
