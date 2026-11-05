# Thin wrapper; the real logic is in packaging/install.sh (works without make).
.PHONY: install uninstall dist test
install uninstall:
	./packaging/install.sh $@
dist:
	./packaging/dist.sh
test:
	PYTHONPATH=src python3 -m unittest discover -s tests
