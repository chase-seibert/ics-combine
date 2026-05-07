PYTHON ?= python3
CONFIG ?= calendars.toml
OUTPUT ?= combined.ics

.PHONY: init-config auth-google list-google-calendars combine push combine-push test clean

init-config:
	@test -f $(CONFIG) || cp calendars.example.toml $(CONFIG)

auth-google: init-config
	$(PYTHON) combine_ics.py auth-google --config $(CONFIG)

list-google-calendars: init-config
	$(PYTHON) combine_ics.py list-google-calendars --config $(CONFIG)

combine: init-config
	$(PYTHON) combine_ics.py combine --config $(CONFIG) --output $(OUTPUT)

push: init-config
	$(PYTHON) combine_ics.py upload --config $(CONFIG) --output $(OUTPUT)

combine-push: init-config
	$(PYTHON) combine_ics.py combine --config $(CONFIG) --output $(OUTPUT) --push-s3

test:
	$(PYTHON) -m unittest discover -s tests -v

clean:
	rm -rf __pycache__ tests/__pycache__ .pytest_cache dist $(OUTPUT)
