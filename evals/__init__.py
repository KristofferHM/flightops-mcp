"""The evaluation harness: how often the agent gets the question right, as a number.

    scenarios.py   golden scenarios: a question, a state of the world, what a right answer is
    fixtures.py    the state of the world, as what the upstreams answer
    answers.py     which class an answer falls in, read from its words
    domain.py      whether a tool's arguments are valid, by schema and by domain
    scoring.py     one scenario's run, scored on each axis
    baseline.py    the committed baseline, and what counts as a regression against it
    report.py      the table, and the block of the README it is written into
    run.py         `uv run python -m evals.run --offline`

What the numbers mean, and what they do not, is in docs/EVALS.md.
"""
