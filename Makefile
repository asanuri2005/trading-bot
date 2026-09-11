.PHONY: sync test build deploy dashboard watchlist
PYTHON ?= python

sync:
	cp strategies.py aws/src/strategies.py
	mkdir -p aws/src/models
	cp models/logreg.json aws/src/models/logreg.json

test: sync
	$(PYTHON) -m unittest discover -s tests -v
	node --test dashboard/src/metrics.test.js

build: test
	cd aws && sam build

deploy: build
	cd aws && sam deploy --guided

dashboard:
	bash aws/scripts/deploy_dashboard.sh

watchlist:
	bash aws/scripts/set_watchlist.sh
