"""Shared helpers for Apify-backed scrapers.

apify-client 3.x changed the return of ``ActorClient.call()``:

  * on success it returns a ``Run`` model (a Pydantic object, **not** a dict),
    so the dataset id is read with attribute access — ``run.default_dataset_id``,
    never ``run["defaultDatasetId"]`` (the model is not subscriptable);
  * when the run fails, is aborted, or times out it returns ``None``.

``dataset_items`` centralises both facts so every scraper reads results the same
way and a failed run surfaces as a clean :class:`ApifyRunError` (which scrapers
turn into their normal error shape) instead of an ``AttributeError`` traceback.
"""


class ApifyRunError(RuntimeError):
    """An Apify actor run did not finish successfully (``call()`` returned ``None``)."""


def dataset_items(client, run) -> list:
    """Return all items from a finished run's default dataset.

    Args:
        client: an ``ApifyClient`` instance.
        run:    the value returned by ``client.actor(...).call(...)`` — a ``Run``
                model on success, or ``None`` if the run failed/aborted/timed out.

    Raises:
        ApifyRunError: if ``run`` is ``None``.
    """
    if run is None:
        raise ApifyRunError("Actor run returned no result (it failed, was aborted, or timed out).")
    return list(client.dataset(run.default_dataset_id).iterate_items())
