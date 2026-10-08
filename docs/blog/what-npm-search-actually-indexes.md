---
layout: post
title: "What npm Search Actually Indexes"
description: "Our 15 keywords did nothing. The README is not indexed. A package with 11 downloads a week outranked us. Here is what we measured, the one field that matters, and the before and after of rewriting it."
date: 2026-10-08
---

csvql-query had 23,641 downloads last month and could not be found on npm by anyone who did not already know its name. Not low-ranked. Absent from the top 25 for every phrase a person would plausibly type.

So we measured what npm's search actually reads, changed one field based on the answer, published, and measured again. The result was three first-place rankings and four queries that did not move at all, which together say more than either would alone.

## The download number was a mirage

Start here, because it changes how you read everything after it. 23,641 a month, 3,447 a week, and this is the per-version breakdown of one week:

```
1.6.0    2277   97.2%     <- published three months ago
2.7.0      19    0.8%
2.5.0       6    0.3%
2.4.2       4    0.2%
```

**97% of our downloads are one old version.** Anyone installing `csvql-query` gets `latest`. That was 19 people. Something is pulling 1.6.0 on a loop, a mirror or a scraper or one pinned CI job, and it has nothing to do with humans choosing the package.

The weekly history shows it as a step, not a ramp:

```
W33-W36    667, 726, 211, 345
W37      5279   <- 15x in one week
W38      6970
W39      7919
```

Nothing shipped in W37. Adoption ramps; this switched on. If you are using npm download counts to decide anything, check `https://api.npmjs.org/versions/<pkg>/last-week` first. The headline number can be almost entirely one pinned version.

## What the search index actually reads

Four fields plausibly feed npm's search: name, description, keywords, README. We probed each by searching for strings that appear in exactly one of them.

| field | indexed | how we know |
|---|---|---|
| name | strongest signal | searching the exact name scores above 1300, against 100-ish for a loose match |
| description | yes, as phrases | three description phrases each ranked 1 out of 1 to 2 million results |
| keywords | **no measurable effect** | `duckdb`, `simd`, `etl`, `dataframe`, `mcp` are all in our keywords; absent from the top 25 for every one |
| README | **not indexed** | three real sentences from our README, all absent |

The keywords result is worth dwelling on, because every package has them and plenty of advice says to tune them. We had fifteen. Searching npm for `duckdb` returns 6,590 packages and ours is not in the first 25, despite `duckdb` being one of our keywords. Same for `simd` with 811 results. Whatever keywords do, they do not put you in results for the keyword.

The README result is the one that costs people the most effort. Searching for `physically cannot modify your data`, a distinctive sentence that appears in our README and nowhere else on npm, returns us nowhere. Write the README for the person who already clicked. Search never sees it.

## Downloads barely matter

This was the assumption we got wrong, and it was worth getting wrong because the correction is encouraging. Here is the top of `csv query` with weekly downloads:

```
rank  score    weekly       package
   1  396.30      13583      @comunica/actor-query-result-serialize-sparql-csv
   2  104.35   22490173      fast-csv
   5  101.64   25066946      csv-parse
  15   78.75         11      csv-analytics-mcp
  23   ~73           23      node-csv-query
```

The top result has 1/1800th the downloads of the one in fifth place and nearly four times the score. A package with **11 downloads a week** is ranked 15th and another with 23 is 23rd, while we, with 23,641 a month, are not in the top 25 at all.

So npm search is winnable at any size. Relevance dominates, and relevance means the name and the description.

## The experiment

Our description was:

```
SQL on CSV files in place — no import, no database. Zig/SIMD engine for Node
and AI agents (MCP). Read-only by design.
```

It ranked us 1st out of 2,097,569 for `no import no database` and 1st out of 1,152,836 for `sql on csv files`. Both useless: nobody types either. Meanwhile the queries people do type were held by packages matching loosely at scores of 104 to 167, while our exact-phrase matches scored 306 to 363. Those slots were open by a factor of two to three.

So we rewrote it to contain the phrases we wanted, and nothing else changed:

```
Run SQL on CSV files in place. Query large CSV files fast with no import and
no database. A CSV SQL engine in Zig with SIMD parsing, for Node and AI
agents (MCP). Read only by design.
```

The prediction was that we would rank for phrases we added and stay absent for phrases we did not. npm reindexes on publish, so this could not be tested before shipping it.

## What happened

```
query                before      after
"run sql on csv"     absent  ->  rank 1  of 1,277,168
"query large csv"    absent  ->  rank 1  of   160,472
"csv sql engine"     absent  ->  rank 1  of   107,918
"query csv"          absent  ->  absent
"csv query"          absent  ->  absent
"fast csv query"     absent  ->  absent
"sql csv"            absent  ->  absent
```

Three phrases added, three first places. `csv sql engine` was held by `pg-promise` at a score of 105; it is now ours at rank 1 of 107,918.

The four that did not move are the useful half of the result. None of them exist as a phrase in the new description. `fast csv query` is the clearest case: the description contains "Query large CSV files fast", which has all four words and still does not rank, because the words are not adjacent in that order. This is phrase matching, not bag-of-words.

`query csv` and `csv query` stay out of reach for a different reason. Two-word generic queries go to packages with those words in the **name**, which is how `node-csv-query` sits at rank 23 on 23 downloads a week. Our distinguishing name token is `csvql`, a word that exists in nobody's vocabulary, and no description can fix that.

## If you are doing this for your own package

- Check your per-version downloads before believing your download count.
- Keywords are not worth tuning. Ours demonstrably do nothing.
- Do not write your README for search. It is not indexed.
- Your description is a phrase index. Spend it on strings people actually type rather than on how you would describe the project to a colleague.
- Decide the phrases first, then write a sentence that happens to contain them and still reads like English. Keyword soup is not required and would not help: you need the phrase, not the density.
- Single generic words go to whoever owns that word as a package name. Do not fight for `csv` or `sql`.

The honest limit: we won three queries and the two highest-volume ones remain unwinnable without a different package name. Whether three specific phrases is worth anything depends entirely on whether anyone searches them, which npm does not tell you. What we can say is that it cost one sentence and the mechanism is now measured rather than guessed.

## Reproducing any of this

```sh
# per-version downloads, the number that matters
curl -s https://api.npmjs.org/versions/<pkg>/last-week | jq .

# where you rank for a phrase
curl -s "https://registry.npmjs.org/-/v1/search?text=<phrase>&size=25" \
  | jq -r '.objects[].package.name' | grep -n <pkg>
```

Figures taken 2026-10-08 against csvql-query 2.9.0. The before figures are from 2026-10-06, against 2.7.0, whose description is in the registry history if you want to check our arithmetic.
