"""
Central config for GTM Engine.

Single source of truth for repo paths, the Claude model, and env-var-backed
credentials. Import this from any scraper or workflow:

    from config import CLAUDE_MODEL, APIFY_TOKEN, CONTEXT_DIR

Repo layout (post-cleanup):
    /Context     — flat .md files describing ICP, product, etc.
    /Scrapers    — one folder per data source (snake_case)
    /Workflows   — one folder per GTM play (snake_case)
"""

import os
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CONTEXT_DIR = os.path.join(REPO_ROOT, "context")
SCRAPERS_DIR = os.path.join(REPO_ROOT, "scrapers")
WORKFLOWS_DIR = os.path.join(REPO_ROOT, "workflows")

load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

CLAUDE_MODEL = "claude-sonnet-4-6"

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
EXA_API_KEY = os.getenv("EXA_API_KEY")
