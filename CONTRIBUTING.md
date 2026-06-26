# Contributing to FLINT

Thanks for taking the time to contribute. FLINT grows through small, friendly
pull requests, and first time contributors are genuinely welcome.

## Ways to help

- Fix a bug or a typo
- Improve the docs
- Add a new tool in `actions/`
- Take on a [good first issue](https://github.com/Hrishikesh2512/FLINT/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
- Test on different Windows setups and report what breaks

## Getting set up

```bash
git clone https://github.com/Hrishikesh2512/FLINT.git
cd FLINT
python setup.py        # installs requirements and Playwright browsers
python main.py         # run it
```

You will need a free Gemini API key from https://aistudio.google.com/apikey.
See the README for where keys live. Never commit a key, the secret files are
already in `.gitignore`.

## Adding a new tool

This is the most common and most welcome contribution, and it is now a single
file:

1. Copy `actions/example_tool.py` to `actions/<your_tool>.py`. It is the
   canonical template; read the docstring for the full walkthrough.
2. Give your tool a unique `name` in the `@tool` decorator, fill in the schema
   (description and parameters), and write the `(args, ctx)` handler.
3. That is all the wiring. The tool is auto-discovered into the registry and
   dispatched by `main.py`; you do not edit `core/tool_registry.py` or
   `main.py`.
4. Test it by voice or text and describe how you tested it in the PR.

## Pull request checklist

- Keep each PR focused on one thing.
- Match the style of the surrounding code.
- Do not commit secrets, build output, or `__pycache__`.
- Explain what you changed and how you verified it.
- Link the issue it closes, if any.

## Reporting bugs and ideas

Open an issue using the templates. For bugs, include your Windows version,
Python version, and the exact steps that triggered the problem. For features,
describe the use case so others can weigh in.

## Code of conduct

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Be kind, assume good intent, and help newcomers.
