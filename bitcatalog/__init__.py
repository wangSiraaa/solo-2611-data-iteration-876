"""bitcatalog -- bit-field message catalog and bus observation warehouse.

Two source modules carry the substance of the bus side:

* :mod:`bitcatalog.catalog` -- definition model, validation (including
  simultaneous-activity / physical-overlap proofs), bit masking, frame
  compilation, payload encoding and compiled-product serialisation.
* :mod:`bitcatalog.warehouse` -- SQLite observation warehouse that binds
  frames to an immutable catalog product and aggregates exact rationals.

:mod:`bitcatalog.reference` is an independent pure-Python bit-list model used
only by tests and cross-checks; it shares no bit arithmetic with the catalog.

The sorting-bench side adds two more modules:

* :mod:`bitcatalog.gauging` -- published dual-gate speed/width convention,
  calibration speed line, exact piecewise width integration, quarantine and
  versioned half-open width classification.
* :mod:`bitcatalog.boxoffice` -- simultaneous change-bag batch planning over
  one physical stock, atomic confirmation in an append-only ledger and
  database-free offline manifest re-checks.
"""

from .catalog import (
    CatalogError,
    CatalogProduct,
    CompileError,
    DecodeError,
    EncodedFrame,
    FrameCodec,
    MessageDef,
    SignalDef,
    SelectorDef,
    build_product,
    load_product,
)
from .warehouse import (
    FrameError,
    FrameEvent,
    FrameIdentity,
    IngestStats,
    ObservationWarehouse,
    SignalAggregate,
    StoredFrame,
    StoredProduct,
)
from .gauging import (
    Calibration,
    GaugeFrame,
    GaugingError,
    GaugingProduct,
    GaugingReport,
    GaugingStation,
    TokenAdmission,
    WidthBand,
    WidthSegment,
    WidthTable,
    recheck_admission,
)
from .boxoffice import (
    BagLine,
    BagOrder,
    BatchPlan,
    Blocker,
    BoxOfficeError,
    BoxOfficeLedger,
    FilledBag,
    InventoryToken,
    plan_batch,
    recheck_manifest,
)

__all__ = [
    "CatalogError",
    "CatalogProduct",
    "CompileError",
    "DecodeError",
    "EncodedFrame",
    "FrameCodec",
    "MessageDef",
    "SignalDef",
    "SelectorDef",
    "build_product",
    "load_product",
    "FrameError",
    "FrameEvent",
    "FrameIdentity",
    "IngestStats",
    "ObservationWarehouse",
    "SignalAggregate",
    "StoredFrame",
    "StoredProduct",
    "GaugingError",
    "GaugingProduct",
    "GaugingStation",
    "GaugingReport",
    "GaugeFrame",
    "Calibration",
    "TokenAdmission",
    "WidthBand",
    "WidthTable",
    "WidthSegment",
    "recheck_admission",
    "BoxOfficeError",
    "BoxOfficeLedger",
    "InventoryToken",
    "BagOrder",
    "BagLine",
    "FilledBag",
    "BatchPlan",
    "Blocker",
    "plan_batch",
    "recheck_manifest",
]

__version__ = "0.2.0"
