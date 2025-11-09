#!/bin/zsh
set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Default values
DO_CLEANUP=false
DO_BUILD=false
EXPLICIT_CLEANUP=false
EXPLICIT_BUILD=false
DEBUG_FLAG=""
PYTHON_FRAMEWORK_DIR=""
HELP=false

# Colors for output (optional, for better UX)
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

# Show usage/help
show_help() {
    cat << EOF
Usage: $0 [OPTIONS]

A build script for PyInstaller CLI bundle using Python 3.13 framework.

Options:
    -h, --help                  Show this help and exit
    -c, --cleanup               Perform cleanup
    -n, --no-cleanup            Skip cleanup (use with -b to build without cleanup)
    -b, --build                 Perform build
    -d, --debug                 Enable debug mode for PyInstaller
    --python-framework-dir DIR  Preset path to Python lib/ dir (e.g., /opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13/lib/)
                                If not set, auto-detects framework.

Examples:
    $0                          # Shows help
    $0 -c                       # Cleanup only
    $0 -b                       # Build only
    $0 -b -c                    # Cleanup + build
    $0 -b -n                    # Build without cleanup
    $0 --debug --python-framework-dir "/opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13/lib/"

EOF
}

# If no arguments, show help
if [[ $# -eq 0 ]]; then
    show_help
    exit 0
fi

# Cleanup function: Remove build artifacts
cleanup() {
    echo -e "${YELLOW}Cleaning up previous build artifacts...${NC}"
    rm -f "${PWD}/finna" "${PWD}"/*.spec
    rm -rf "${PWD}/build" "${PWD}/dist"
    echo -e "${GREEN}Cleanup complete.${NC}"
}

# Setup venv and install dependencies
setup_venv_and_deps() {
    local python_bin pip_cmd

    # Detect Python 3.13
    python_bin=$(command -v python3.13 || command -v python3)
    if [[ -z "$python_bin" ]]; then
        echo -e "${RED}python3.13 not found. Install via Homebrew and retry.${NC}"
        exit 1
    fi
    echo -e "${GREEN}Using Python: $python_bin${NC}"

    # Setup venv if missing
    if [[ ! -d ".venv" ]]; then
        echo -e "${YELLOW}Creating virtual environment...${NC}"
        "$python_bin" -m venv .venv
    fi
    source .venv/bin/activate

    # Detect pip
    if command -v pip3 &> /dev/null; then
        pip_cmd="pip3"
    elif command -v pip &> /dev/null; then
        pip_cmd="pip"
    else
        echo -e "${RED}pip is not installed. Please install pip and try again.${NC}"
        exit 1
    fi

    # Install requirements
    echo -e "${YELLOW}Installing dependencies from requirements.txt...${NC}"
    if ! "$pip_cmd" install -r requirements.txt; then
        echo -e "${RED}Failed to install dependencies. Check requirements.txt and network.${NC}"
        exit 1
    fi

    # Install PyInstaller if needed
    if ! command -v pyinstaller &> /dev/null; then
        echo -e "${YELLOW}pyinstaller could not be found, installing it now...${NC}"
        "$pip_cmd" install pyinstaller
    fi
}

# Build function
build() {
    local python_bin python_framework libpython python_exec

    python_bin=$(command -v python3.13 || command -v python3)
    if [[ -z "$PYTHON_FRAMEWORK_DIR" ]]; then
        python_framework=$(dirname "$(dirname "$python_bin")")/Frameworks/Python.framework/Versions/3.13
        echo -e "${GREEN}Auto-detected Python framework: $python_framework${NC}"
    else
        python_framework=$(dirname "$PYTHON_FRAMEWORK_DIR")
        echo -e "${GREEN}Using custom Python framework: $python_framework${NC}"
    fi

    libpython="$python_framework/lib/libpython3.13.dylib"
    python_exec="$python_framework/Python"
    if [[ ! -f "$libpython" || ! -f "$python_exec" ]]; then
        echo -e "${RED}Python 3.13 framework not detected at $python_framework. Check your install.${NC}"
        exit 1
    fi

    echo -e "${YELLOW}Building with PyInstaller...${NC}"
    pyinstaller cli.py \
        --distpath=dist/cli \
        --add-binary "$libpython:_internal" \
        --add-binary "$python_exec:_internal" \
        $DEBUG_FLAG

    # Verify build
    if [[ ! -f "dist/cli/cli/cli" ]]; then
        echo -e "${RED}Build failed: Executable not found in dist/cli/cli/.${NC}"
        exit 1
    fi
    # we must have PWD/finna as a dir
    if [ -d "${PWD}/finna" ]; then
        rm -rf "${PWD}/finna"
    fi
    mkdir ${PWD}/finna

    cp -r "${PWD}/dist/cli/cli/" "${PWD}/finna/"

    rm -r "${PWD}/dist" "${PWD}/build"

    # if cli.spec exists at pwd, remove
    if [ -f "${PWD}/cli.spec" ]; then
        rm "${PWD}/cli.spec"
    fi

    echo -e "${GREEN}Build successful! Test with: ./finna/cli${NC}"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            HELP=true
            shift
            ;;
        -c|--cleanup)
            DO_CLEANUP=true
            shift
            ;;
        -n|--no-cleanup)
            DO_CLEANUP=false
            shift
            ;;
        -b|--build)
            DO_BUILD=true
            shift
            ;;
        -d|--debug)
            DEBUG_FLAG="--debug=all"
            shift
            ;;
        --python-framework-dir)
            PYTHON_FRAMEWORK_DIR="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            show_help
            exit 1
            ;;
    esac
done

# Show help if requested
if [[ $HELP == true ]]; then
    show_help
    exit 0
fi

# Execute phases
if [[ $DO_CLEANUP == true ]]; then
    cleanup
fi

if [[ $DO_BUILD == true ]]; then
    setup_venv_and_deps
    build
fi

if [[ $DO_CLEANUP == false && $DO_BUILD == false ]]; then
    echo -e "${YELLOW}No actions specified. Use --help for options.${NC}"
    exit 0
fi