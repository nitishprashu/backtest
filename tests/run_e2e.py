"""Spawn the test server, run the browser flow, tear everything down (all in one process tree)."""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
port = sys.argv[1] if len(sys.argv) > 1 else "8799"
srv = subprocess.Popen([sys.executable, os.path.join(HERE, "serve_fake.py"), port, "2025-01-01", "2025-03-07"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    t0 = time.time()
    line = srv.stdout.readline()
    print("server:", line.strip(), f"({time.time()-t0:.0f}s)", flush=True)
    which = sys.argv[2] if len(sys.argv) > 2 else "e2e.py"
    r = subprocess.run([sys.executable, os.path.join(HERE, which), f"http://127.0.0.1:{port}/", "/tmp/shots"], text=True, timeout=240)
finally:
    srv.kill()
