#!/usr/bin/env bash
# Usage: OPENROUTER_API_KEY=sk-or-... [ALERT_EMAIL=you@example.com] ./deploy.sh
set -euo pipefail
STACK="${STACK:-voice-chatbot}"
REGION="${AWS_REGION:-us-east-1}"
: "${OPENROUTER_API_KEY:?Set OPENROUTER_API_KEY first}"

sam build
sam deploy --stack-name "$STACK" --region "$REGION" --resolve-s3 \
  --capabilities CAPABILITY_IAM --no-confirm-changeset --no-fail-on-empty-changeset \
  --parameter-overrides "OpenRouterApiKey=$OPENROUTER_API_KEY" ${ALERT_EMAIL:+"AlertEmail=$ALERT_EMAIL"}

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
echo "window.CONFIG = { API_URL: \"$(out ApiUrl)\" };" > web/config.js
aws s3 sync web "s3://$(out WebBucketName)" --delete --region "$REGION"
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" > /dev/null
echo "Deployed: $(out SiteUrl)"
