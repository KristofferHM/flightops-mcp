/* What the board keeps of answers it already has: the last few views, and the upstream's clock
   to age them by. Its own file so the browser gate can test it without driving the map.
   Nothing here changes a payload. A kept answer is the same object, with its own timestamps. */
(() => {
  "use strict";

  // The newest few board answers by region and view. A Map keeps insertion order, so the
  // first key is the least recently used one.
  class Views {
    constructor(limit) {
      this.limit = limit;
      this.entries = new Map();
    }

    get size() { return this.entries.size; }

    keys() { return [...this.entries.keys()]; }

    // `heard` is the page's monotonic clock when the answer arrived.
    put(key, payload, heard) {
      this.entries.delete(key);
      this.entries.set(key, { key, payload, heard });
      while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
    }

    // A view looked up again is the most recently used.
    get(key) {
      const entry = this.entries.get(key);
      if (!entry) return null;
      this.entries.delete(key);
      this.entries.set(key, entry);
      return entry;
    }

    setLimit(limit) {
      this.limit = limit;
      while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
    }

    clear() { this.entries.clear(); }
  }

  const epoch = (value) => (Number.isInteger(value) && value > 0 ? value : null);

  // The upstream's clock now, counted on from answers' `upstream_epoch` by the page's
  // monotonic clock, passed in as `at`. Never the time of day: this machine's clock has run
  // 21 minutes fast. The furthest-ahead reading wins, so an answer served from the tools'
  // cache, which carries an older upstream time, cannot pull the clock back.
  class Clock {
    constructor() { this.offset = null; }

    hear(value, at) {
      const known = epoch(value);
      if (known === null) return;
      const offset = known - at / 1000;
      if (this.offset === null || offset > this.offset) this.offset = offset;
    }

    now(at) { return this.offset === null ? null : at / 1000 + this.offset; }

    // Seconds since `value` by the upstream's clock, or null when either is unknown.
    age(value, at) {
      const known = epoch(value);
      const now = this.now(at);
      return known === null || now === null ? null : Math.max(0, now - known);
    }
  }

  // A kept answer within the TTL may be painted as the view. Without an age it never is.
  const fresh = (age, ttl) => age !== null && age <= ttl;

  window.BoardCache = { Views, Clock, fresh };
})();
