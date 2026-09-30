# Moving the site to csvql.birim.one.dev

**Do not merge this until the DNS record exists.** A `CNAME` file pointing at a
domain with no DNS behind it takes the current site down: GitHub stops serving
`melihbirim.github.io/csvql/` and the new name does not resolve yet.

## Order of operations

1. **Add the DNS record** at whoever hosts `birim.one.dev`:

   ```
   Type   Name    Value
   CNAME  csvql   melihbirim.github.io.
   ```

   Note the trailing dot. Propagation is usually minutes.

2. **Check it resolves** before merging:

   ```sh
   dig +short csvql.birim.one.dev      # should show melihbirim.github.io
   ```

3. **Merge this PR.** GitHub Pages picks up `docs/CNAME` and serves the new name.

4. **Turn on HTTPS.** Settings, Pages, "Enforce HTTPS". The certificate can take
   up to an hour to issue; the checkbox is greyed out until it does.

5. **Re-submit to Google Search Console** as a new property. The old property
   does not carry over, and this is the step that actually matters: the site is
   not in the index today, so there is nothing to lose by moving and everything
   to gain by submitting the domain you own.

## Why bother

`github.io` is on the Public Suffix List, so `melihbirim.github.io` is treated as
a separate site that inherits no authority from github.com and starts from zero.
Every post published there builds authority for a subdomain you are renting.
On `birim.one.dev` it accrues to a domain you own, and it survives any future
move off GitHub Pages.

## What this changes

- `docs/CNAME` added
- `docs/_config.yml`: `url` and `baseurl` (baseurl becomes empty, since the site
  is now at the root of its own subdomain rather than in a `/csvql` path)
- `docs/robots.txt` and `README.md`: absolute URLs updated

GitHub keeps redirecting `melihbirim.github.io/csvql/` to the new domain, so the
existing README link and any shared post links keep working.
