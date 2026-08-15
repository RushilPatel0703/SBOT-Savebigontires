"""Re-exports all helpers from pipelines/shared/config.py.
All formula scripts import from here unchanged — no edits needed in individual scripts.
"""
import importlib.util
import os

_shared_path = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared", "config.py")
)
_spec = importlib.util.spec_from_file_location("_shared_config", _shared_path)
_m    = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)

SHOP            = _m.SHOP
TOKEN           = _m.TOKEN
API_VERSION     = _m.API_VERSION
SERPAPI_KEY     = _m.SERPAPI_KEY
HANDLE_MAP_FILE = _m.HANDLE_MAP_FILE
SKU_MAP_FILE    = _m.SKU_MAP_FILE
find_latest     = _m.find_latest
HEADERS         = _m.HEADERS
api_get         = _m.api_get
api_put         = _m.api_put
api_delete      = _m.api_delete
api_post        = _m.api_post
get_logger      = _m.get_logger
