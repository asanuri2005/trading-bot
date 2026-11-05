# Project guide

## Three parts

The research tools replay trading rules on historical prices. The AWS functions collect daily signals, reconcile an Alpaca paper portfolio, and value seven simulated books. The React dashboard displays the published comparison and broker order records.

For a quick look, follow the [account-free dashboard setup](README.md#try-it-without-accounts). For technical details, read [the accounting and execution review](docs/ACCOUNTING_AND_EXECUTION.md).

## Understanding the dashboard

Each book begins with $12,000. Its equity is cash plus the value of its synthetic holdings; its cumulative percentage is equity / $12,000 - 1. The leaderboard and chart legend rank that percentage. A difference between two returns is measured in **percentage points**.

The cards and chart are simulated books with modeled costs. The account equity and order table come from the combined Alpaca paper account. The six active strategies contribute to that account; buy-and-hold is a reference only. Different sizing, prices, and execution timing mean the simulated and broker results will differ.

The page fetches its data file every minute, but the reporter normally publishes once after the market closes. A refreshed page does not imply new market data. `?sample=1` explicitly selects fictional local sample data; it never falls back to sample data after a live fetch failure.

## Deploy a separate AWS instance

Use Python 3.12 for the Lambda build, Node.js 22, AWS CLI v2, and AWS SAM CLI. The shell examples below use Bash; Windows users can use Git Bash. `aws --version` and `sam --version` must work before continuing. If an installed CLI is not found, reopen the terminal so it picks up PATH changes.

1. Configure AWS authentication for the target account and region. Check the selected identity with `aws sts get-caller-identity`. Use your own credentials locally; never commit them.
2. Create Alpaca **paper** API credentials. Store the key ID at `/trading-bot/alpaca/key_id` and secret at `/trading-bot/alpaca/secret` as SSM **SecureString** values in the deployment region. The AWS console lets you do this without putting values in shell history. In Git Bash, prefix commands involving SSM paths with `MSYS_NO_PATHCONV=1` to avoid Windows path conversion.
3. Edit `watchlist.txt` for the desired universe, then run the local tests in [README.md](README.md#run-the-research-and-checks).
4. Copy the shared code and model into the Lambda package:

```bash
cp strategies.py aws/src/strategies.py
cp models/logreg.json aws/src/models/logreg.json
cd aws
sam build
sam deploy --guided
```

During guided deployment, choose your stack name and region. Supply the watchlist as a comma-separated string, a new `ExperimentId`, and keep `DryRun=true` for the first deployment. The template defaults to a $12,000 book budget and 5 bps per side. Its `MaxOrderUsd` is a separate per-order limit, not a portfolio budget.

Choose a Python 3.12 pandas layer ARN for your region from the [AWS SDK for pandas layer list](https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html). The template's ARN is a historical default: check availability before deployment. A cross-account layer permission failure is not fixed by granting your IAM user more permissions to a layer it does not own.

The template creates the SSM watchlist, DynamoDB table, Lambda functions and roles, schedules, dashboard bucket, and CloudFront distribution. The two secret parameters must already exist. The fixed SSM names mean multiple deployments in one account and region need additional naming changes.

From the repository root, build and publish the frontend:

```bash
STACK_NAME=your-stack-name bash aws/scripts/deploy_dashboard.sh
```

This script resolves the stack outputs, builds the dashboard, uploads its assets, and invalidates CloudFront. It preserves the reporter's `data.json`. Before the first successful report, the page may show that no data has been published.

## Operate and change an experiment

Review the Lambda logs and complete daily records before enabling paper orders. The executor requires a fresh complete signal batch, matching configuration, an active paper account, no unresolved open orders, and available cash. A ticker without a recent trade price waits for the next run while the rest trade. See [the paper-launch record](docs/PAPER_LAUNCH_2026-09.md) for the dated checks used for the existing deployment; those checks do not automatically validate a new account.

When ready for paper execution, deliberately set `DryRun=false` in your deployment parameters and redeploy. Keep the account dedicated to this bot. The code rejects live-money endpoints; live trading requires a separate implementation review.

`watchlist.txt` is not watched continuously. To preview a change in Git Bash:

```bash
DRY_RUN=1 bash aws/scripts/set_watchlist.sh
```

The script without `DRY_RUN` publishes the list to SSM and updates the local saved SAM override when present. A changed universe also requires a **new experiment ID and deployment**. Until those agree, the existing experiment rejects the change. Coordinate this while no scheduled run is executing. Changes to book budget, transaction costs, strategy lineup, or model also need a new experiment.

Old experiment history is retained. Do not delete an inconvenient first day to improve comparisons: start a new experiment with common settings instead. A missed valuation session can be replayed from its stored signal batch in chronological order; a missing signal batch cannot be reconstructed by the reporter.

## ML development

`models/logreg.json` is the frozen model used for inference, with a matching copy under `aws/src/models/`. Running `ml_train.py` writes a separate candidate by default. It does not replace the deployed model or alter the paper experiment.

Compare candidates on common unseen dates and account for modeled costs, idle cash, drawdowns, and missing predictions. A better result on one asset or period is not enough to establish a robust improvement. Promotion should be explicit, with the artifact copied into the package, tests rerun, and a new experiment ID chosen.

## What is and is not automated

GitHub Actions runs the Python regression suite, checks packaged artifact consistency, runs an offline portfolio smoke test, tests dashboard return ranking, and builds the frontend. It does not deploy, train a replacement model, or access brokerage credentials.

AWS runs the configured market-day schedule after manual deployment. Dashboard releases, ticker changes, experiment changes, and model promotion remain explicit operator actions.
