.PHONY: setup watch run demo test
setup:
	@test -f .env || cp .env.example .env; echo "Edit .env and set CRUSTDATA_API_KEY"
watch:
	python3 pitch.py watch
run:
	python3 pitch.py pitch
demo:
	python3 pitch.py pitch --dry-run --all
test:
	python3 -m unittest discover -s tests -v
