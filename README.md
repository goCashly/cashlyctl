# CashlyCTL

Command line utility for interacting with the Cashly API.

## Installation

```bash
pip install .
```

## Usage

### Borrower
Create a borrower:
```bash
cashlyctl borrower create --first-name John --last-name Doe --annual-income 75000
```

### Pathfinder
Run a Pathfinder query:
```bash
cashlyctl pathfinder --id-mortgage-app PAPLx15855
```

### Configuration

Set `CASHLY_API_URL` to point to a custom API endpoint if needed:
```bash
export CASHLY_API_URL="http://localhost:8000"
```

