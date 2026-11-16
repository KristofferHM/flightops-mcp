"""The agent loop: the proof that the governed server is usable, and stoppable.

Thin on purpose. The server is the product; this package plans, calls tools,
reads the results and writes down what happened. Three things it does not do:

* **It does not approve.** A write stops at the server's approval request, and
  the request goes to an `Approver` the model never sees (docs/adr/0003).
* **It does not borrow.** Its budget is its own. The server's upstream credit
  ledger belongs to the operator and is never read or adjusted from here.
* **It does not improvise time or chance.** The clock is injected, there is no
  `random`, and the offline backend is a function of its scenario.

    uv run python -m agent run --scenario rebook-approved --backend offline
"""

from __future__ import annotations
