# cashlyctl

Command-line utility for interacting with Cashly services.

## Configuration

`cashlyctl` reads configuration from the environment, including values loaded
from a local [`.env`](https://pypi.org/project/python-dotenv/) file. Create a
`.env` file in the same directory where you run the CLI (or export variables in
shell) to set the API base URL and optional API key:

```dotenv
CASHLY_API_URL=http://localhost:8000
CASHLY_API_KEY=super-secret-key
```

At runtime `cashlyctl` automatically calls `load_dotenv()` so the values above
are available without additional setup. When `CASHLY_API_KEY` is present the
shared HTTP session attaches it as an `X-API-KEY` header on every request.

## Development

Install dependencies in editable mode:

```bash
pip install -e .[dev]
```

Then run the CLI:

```bash
cashlyctl --help
```
