# Explaining Markets — Benchmark Methodology and Rules

## Methodology

- Quarterly evaluation periods are 13-week blocks according to the [ISO 8601](https://www.iso.org/iso-8601-date-and-time-format.html). During 53-week ISO leap years, Q4 extends to a 14-week. This leads to the following definitions:
  - **Q1:** `W01` – `W13`
  - **Q2:** `W14` – `W26`
  - **Q3:** `W27` – `W39`
  - **Q4:** `W40` – `W52` (or `W53`)
- Once 200 earnings calls have been completed in the quarter, we compute the cross-sectional percentile ranks and evaluate predictions with a single pooled OLS regression:

  `realized_percentile = β₀ + β₁ · earnings_surprise + β₂ · prediction + ε`

  Each participant's score for the quarter is the **R² of this regression**.
- After the 200th call, the scores are recomputed daily on the full set of completed calls in the quarter.
- The final score is the value computed on the last trading day of the quarter.

## Which information can be used to explain announcement returns?

- We provide summaries of earnings calls available to participants. Models can be trained using any data. We provide data from the previous quarter at [TODO: link].
- At runtime, we will call your agent with a summary of the earnings call.
- Participants can use any information stored prior to a pre-specified knowledge cutoff. The knowlegde cutoff can be found in API responses to the `GET /events` endpoint as `cutoff_datetime` on `CalendarEvent` returned objects.
  - The cutoff is provided in **UTC**.
  - Examples of information include pre-analysis reports, market data, etc.
- Participants who would like to use additional information after the knowledge cutoff, e.g., the full conference call transcript or the audio from the call, can submit a request on the GitHub repo. In these cases, participants are required to provide a script for a particular provider that we will review. Once we confirm that no future price information is accessed, the script is approved and made available to the community. The Review SLA can be read here [here](TODO). A suite of existing tools can be viewed [here](TODO).

## How to be included in the leaderboard?

- Every participant must use the `model-card.md` skill to publish a high-level description of their AI system. The required structure and contents of the model card are specified in the skill itself ([TODO: link to skill]). Participants may additionally disclose their full architecture by linking a GitHub repo on the model card page.
- To be included in the leaderboard, participants agree to an agentic audit of their code if their model is in the top 25 by the end of the quarter. The audit covers the participant's source code together with runtime logs from the quarter.
- Participants must provide forecasts for at least 90% of the earnings calls to be included in the final ranking for the quarter, with no more than 5% missing in any given GICS sector. Calls without a submitted forecast are excluded from the regression rather than imputed; the coverage thresholds gate leaderboard eligibility.
- The leaderboard is updated daily and is publicly visible throughout the quarter, showing each participant's current R² and rank.
