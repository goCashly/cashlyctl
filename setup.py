# setup.py
from pathlib import Path
from setuptools import find_packages, setup

# --------------------------------------------------------------------------- #
# Basic metadata
# --------------------------------------------------------------------------- #
NAME = "cashlyctl"
DESCRIPTION = "Cashly command‑line utility for quick admin and developer workflows."
URL = "https://github.com/your‑org/cashlyctl"          # adjust if you publish
AUTHOR = "Cashly"
AUTHOR_EMAIL = "operations@gocashly.io"
LICENSE = "MIT"
PY_VERSION = ">=3.12"

# --------------------------------------------------------------------------- #
# Helper – read README if present
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).parent
readme_path = ROOT / "README.md"
LONG_DESC = readme_path.read_text(encoding="utf‑8") if readme_path.exists() else DESCRIPTION

# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
REQUIRED = [
    "typer[all]>=0.12.0",
    "click>=8.1",
    "requests>=2.32.0",
    "textual~=0.58.0",
    "pyfiglet>=1.0.0",
    "python-dotenv>=1.0.0",
]

EXTRAS = {
    "dev": ["pytest", "black", "ruff"],
}

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
setup(
    name=NAME,
    version="0.1.0",
    description=DESCRIPTION,
    long_description=LONG_DESC,
    long_description_content_type="text/markdown",
    author=AUTHOR,
    author_email=AUTHOR_EMAIL,
    url=URL,
    license=LICENSE,
    python_requires=PY_VERSION,
    packages=find_packages(exclude=("tests",)),
    install_requires=REQUIRED,
    extras_require=EXTRAS,
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "cashlyctl = cashlyctl.cli:_main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3 :: Only",
        "License :: OSI Approved :: MIT License",
        "Environment :: Console",
        "Intended Audience :: Developers",
    ],
    project_urls={
        "Source": URL,
    },
)
