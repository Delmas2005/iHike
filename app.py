"""Entry point of the Hugging Face Space: `python app.py` serves iHike on port 7860.

The code lives in the `ihike` package; see docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import os

import uvicorn

from ihike.web import app

if __name__ == "__main__":
    # Hugging Face sends visitors to port 7860 (GRADIO_SERVER_PORT on Gradio Spaces).
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GRADIO_SERVER_PORT", 7860)))
