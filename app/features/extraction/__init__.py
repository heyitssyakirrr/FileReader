from app.features.extraction.router import router
from app.features.extraction.pipeline import start_ocr_worker
from app.features.extraction.concurrency import get_pending_tasks
from app.features.extraction.lifecycle import drain_and_finalize