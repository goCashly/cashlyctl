# CashlyCTL

CashlyCTL is a terminal-based interface for exploring, editing, and managing the mortgage graph.
It uses the [Textual](https://github.com/Textualize/textual) framework to provide a fast, interactive TUI.

## Features

- File browser panel for navigating Lender Products and Mortgage Applications.
- File viewer with syntax highlighting and auto-scaling to fit the terminal window.
- Inline editing with JSON validation and keyboard shortcuts.
- Command line interface for opening, editing, and saving files.
- Log panel that records all actions and command outputs.

## Current Key Bindings

| Shortcut | Action |
|-----------|---------|
| **Ctrl + S** | Save changes in edit mode |
| **Ctrl + X** | Close current editor without saving |
| **Esc** | Cancel edit mode |
| **Q** | Quit the application |

## Available Commands

| Command | Description |
|----------|-------------|
| `open <filename>` | Opens a JSON file for viewing |
| `edit <filename>` | Opens a file directly in edit mode |
| `edit` | Enters edit mode for the currently open file |
| `save` | Saves current edits (with JSON validation) |
| `refresh` | Reloads the file tree |

## Folder Structure

```
cashlyctl/
├── main.py
├── ui.py
├── controllers/
│   ├── base.py
│   ├── files.py
│   ├── system.py
│   └── __init__.py
├── widgets/
│   ├── filetree.py
│   ├── jsonviewer.py
│   ├── logview.py
│   ├── keyhelp.py
│   └── __init__.py
└── FILES/
    └── (JSON subfolders and files)
```

## Setup

1. Clone the repository:

```bash
git clone https://github.com/goCashly/cashlyctl.git
cd cashlyctl
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the application:

```bash
python -m cashlyctl.main
```

## Notes

- Only `.json` files are currently loaded in the viewer.
- Non-JSON files will display a warning.
- JSON validation is performed automatically before saving.
