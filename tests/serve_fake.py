"""Start the app against the in-memory test market (for browser tests / screenshots only)."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import MemoryProvider
from lab.runs import RunStore
from lab.server import create_server
from lab.service import Service

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
start, end = (sys.argv[2], sys.argv[3]) if len(sys.argv) > 3 else ("2025-01-01", "2025-04-30")
tmp = tempfile.mkdtemp()
srv = create_server(Service(MemoryProvider(start, end, seed=5, strikes_each_side=12), RunStore(os.path.join(tmp, "r.db"))), port)
print("READY", srv.server_address[1], flush=True)
srv.serve_forever()
