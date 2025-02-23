#!/bin/bash -e

# This script drives the various tasks involved in testing and building the
# various artifacts for the Pbench product.  It is intended to be run from the
# root directory of a Git branch checkout.


# Install the linter requirements and add them to the PATH.
export PATH=${HOME}/.local/bin:${PATH}
python3 -m pip install --user -r lint-requirements.txt

# If this script is run in a container and the user in the container doesn't
# match the owner of the Git checkout, then Git issues an error; these config
# settings avoid the problem.
GITTOP=$(git rev-parse --show-toplevel 2>&1 | head -n 1)
if [[ ${GITTOP} =~ "fatal: unsafe repository ('/home/root/pbench'" ]] ; then
	git config --global --add safe.directory /home/root/pbench
	GITTOP=$(git rev-parse --show-toplevel)
fi

# Install the Dashboard dependencies, including the linter's dependencies and
# the unit test dependencies.  First, remove any existing Node modules and
# package-lock.json to ensure that we install the latest.
( cd dashboard && rm -rf node_modules package-lock.json && npm install )

# Test for code style and lint
black --check .
flake8 .
isort --check .
( cd dashboard && npx eslint "src/**" --max-warnings 0 )

# Run unit tests
tox                                     # Agent and Server unit tests and legacy tests
( cd dashboard && CI=true npm test )    # Dashboard unit tests

exit 0
