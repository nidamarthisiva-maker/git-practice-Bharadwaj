#!/usr/bin/env bash
# usage: ./loadgen.sh            -> 60s of healthy traffic
#        ./loadgen.sh slow       -> one slow request (db problem)
#        ./loadgen.sh fail       -> a burst of failing requests (payment down)
B=http://localhost:5000
case "$1" in
  slow) for i in 1 2 3; do curl -s -o /dev/null -w "slow_db  -> %{http_code} in %{time_total}s\n" "$B/api/checkout?scenario=slow_db"; done ;;
  fail) for i in $(seq 1 15); do curl -s -o /dev/null -w "payment_down -> %{http_code} in %{time_total}s\n" "$B/api/checkout?scenario=payment_down"; done ;;
  *)    end=$((SECONDS+60)); while [ $SECONDS -lt $end ]; do
          curl -s -o /dev/null "$B/api/orders"; curl -s -o /dev/null "$B/api/checkout"; sleep 0.2; done; echo done ;;
esac
