"""Re-exports all helpers from pipelines/shared/config.py."""
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
HANDLE_MAP_FILE = _m.HANDLE_MAP_FILE
SKU_MAP_FILE    = _m.SKU_MAP_FILE
find_latest     = _m.find_latest
HEADERS         = _m.HEADERS
api_get         = _m.api_get
api_put         = _m.api_put
api_delete      = _m.api_delete
api_post        = _m.api_post
get_logger      = _m.get_logger
markup_for        = _m.markup_for
price_with_markup = _m.price_with_markup
price_floor       = _m.price_floor
describe_markup   = _m.describe_markup
load_markups      = _m.load_markups

# Canonical SKU helpers live in shared/sku_normalize.py; re-exported here so the
# wheelsmart scripts import them the same way they import everything else shared.
_skun_path = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared", "sku_normalize.py")
)
_skun_spec = importlib.util.spec_from_file_location("_shared_sku_normalize", _skun_path)
_skun      = importlib.util.module_from_spec(_skun_spec)
_skun_spec.loader.exec_module(_skun)

normalize_sku      = _skun.normalize_sku
disambiguate_skus  = _skun.disambiguate_skus
