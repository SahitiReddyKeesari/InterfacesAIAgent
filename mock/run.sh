#!/usr/bin/env bash
# Compile and run the mock back-office. Requires a JDK 17+.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# macOS ships a /usr/bin/javac stub that exists but fails when no JDK is installed,
# so a candidate is only accepted if it actually runs.
works() { [[ -x "$1" ]] && "$1" -version >/dev/null 2>&1; }

javac=""; java=""
for home in "${JAVA_HOME:-}" "$HOME"/.jdks/jdk-*/Contents/Home "$HOME"/.jdks/jdk-*; do
    if works "$home/bin/javac"; then
        javac="$home/bin/javac"; java="$home/bin/java"; break
    fi
done

if [[ -z "$javac" ]] && works "$(command -v javac || true)"; then
    javac=$(command -v javac); java=$(command -v java)
fi

if [[ -z "$javac" ]]; then
    echo "No working JDK found. See mock/README.md for the one-line install." >&2
    exit 1
fi

"$javac" -d "$here/out" "$here"/src/*.java
exec "$java" -cp "$here/out" MockBank
