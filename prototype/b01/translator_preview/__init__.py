"""B02 isolated text preview; the production SwiftUI App remains separate."""

PRODUCT_VERSION = "0.2.13"
PREVIEW_ID = "t09b-r13.1-whisper.1"
# Reading/opening never connects to a model. T05 inference uses an explicitly
# registered, owned endpoint; there is no fallback to the shared service.
DEFAULT_ENDPOINT = "http://127.0.0.1:1"
