#!/usr/bin/env bash
# Build the dashboard and publish it to the stack's S3 bucket + CloudFront.
# Usage: make dashboard   (or STACK_NAME=my-stack bash aws/scripts/deploy_dashboard.sh)
set -euo pipefail
export MSYS_NO_PATHCONV=1
STACK="${STACK_NAME:-trading-bot}"

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET=$(out DashboardBucketName)
DIST=$(out DashboardDistributionId)
URL=$(out DashboardURL)

cd "$(dirname "$0")/../../dashboard"
npm ci --no-audit --no-fund
npm run build

# --exclude data.json: never delete the reporter's published data
# Keep old hashed assets for already-open pages and cached index responses.
aws s3 sync dist/ "s3://$BUCKET/" --exclude data.json
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*" >/dev/null

echo "Dashboard live at: $URL"
