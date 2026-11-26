<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Contributing guidelines

Thank you for your interest in contributing to this project. Bug reports,
feature ideas, corrections, and documentation improvements are welcome.

Read this document before submitting an issue or pull request so your
contribution includes the information needed for review.

## Report bugs or request features

Use the GitHub issue tracker to report bugs or suggest features. Before opening
an issue, check open and recently closed issues for an existing report. Include
steps to reproduce, expected and actual behavior, and relevant environment
details when available.

## Contribute through pull requests

Before opening a pull request:

1. Start from the latest source on the *main* branch.
2. Check open and recently merged pull requests for related work.
3. Open an issue before beginning a significant change so the scope and approach
   can be discussed first.

To submit a pull request:

1. Fork the repository.
2. Make a focused change in your fork.
3. Run the relevant local tests and checks.
4. Commit with a clear message.
5. Open a pull request and complete the requested description fields.
6. Address automated checks and review feedback.

GitHub provides additional documentation on [forking a repository](https://help.github.com/articles/fork-a-repo/)
and [creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## Run the tests

The app's unit lane,
from `agentic_digital_thread/` (install `requirements.txt` and
`pip install -e 'agentic_digital_thread[test]'` first):

```bash
PYTHONPATH=. python -m pytest tests/unit tests/test_orchestrator_memory.py -q \
  --disable-socket --allow-unix-socket -n auto
```

The repo-root suites (CDK synth and stack wiring) run from the repository root
with `python -m pytest tests/ -q`, after `npm ci` for the local CDK CLI. The web
UI runs `npx tsc --noEmit`, `npx vitest run` and `npm run build` from
`agentic_digital_thread/frontend/`.

## Find contributions to work on

Review the open issues to find changes that are ready for contribution.

## Code of conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information, see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq)
or contact opensource-codeofconduct@amazon.com.

## Security issue notifications

If you discover a potential security issue, notify AWS/Amazon Security through
the [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).
Do **not** create a public GitHub issue.

## Licensing

See [LICENSE](LICENSE) for the project license. Contributors may be asked to
confirm the licensing of their contributions.
