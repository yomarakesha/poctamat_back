#!/usr/bin/env bash
# Offline APK build using the raw Android SDK toolchain (no Gradle, no internet).
# Needs: build-tools 34.0.0, platform android-34, JDK 17.
set -euo pipefail

SDK="/c/Users/yomarakesha/AppData/Local/Android/Sdk"
BT="$SDK/build-tools/34.0.0"
ANDROID_JAR="$SDK/platforms/android-34/android.jar"

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build"
GEN="$BUILD/gen"
OBJ="$BUILD/obj"
OUT_UNSIGNED="$BUILD/app-unsigned.apk"
OUT_ALIGNED="$BUILD/app-aligned.apk"
OUT="$HERE/locker-kiosk.apk"
KS="$HERE/debug.keystore"

rm -rf "$BUILD"; mkdir -p "$GEN" "$OBJ"

echo "== 1. compile resources =="
"$BT/aapt2.exe" compile --dir "$HERE/res" -o "$BUILD/res.zip"

echo "== 2. link resources -> base apk + R.java =="
"$BT/aapt2.exe" link \
  -o "$OUT_UNSIGNED" \
  -I "$ANDROID_JAR" \
  --manifest "$HERE/AndroidManifest.xml" \
  --java "$GEN" \
  --min-sdk-version 21 --target-sdk-version 34 \
  "$BUILD/res.zip"

echo "== 3. javac =="
find "$HERE/src" "$GEN" -name '*.java' | while read -r f; do cygpath -w "$f"; done > "$BUILD/sources.txt"
javac -d "$(cygpath -w "$OBJ")" -classpath "$(cygpath -w "$ANDROID_JAR")" -source 17 -target 17 @"$(cygpath -w "$BUILD/sources.txt")"

echo "== 4. d8 -> classes.dex =="
CLASSES=$(find "$OBJ" -name '*.class')
"$BT/d8.bat" --lib "$ANDROID_JAR" --min-api 21 --output "$BUILD" $CLASSES

echo "== 5. add classes.dex into apk =="
W_UNSIGNED="$(cygpath -w "$OUT_UNSIGNED")"
W_ALIGNED="$(cygpath -w "$OUT_ALIGNED")"
W_DEX="$(cygpath -w "$BUILD/classes.dex")"
py -c "import zipfile,shutil; shutil.copy(r'$W_UNSIGNED', r'$W_ALIGNED'); z=zipfile.ZipFile(r'$W_ALIGNED','a',zipfile.ZIP_DEFLATED); z.write(r'$W_DEX','classes.dex'); z.close(); print('dex added')"

echo "== 6. zipalign =="
"$BT/zipalign.exe" -f 4 "$OUT_ALIGNED" "$BUILD/app-final.apk"

echo "== 7. keystore (create if missing) =="
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias kiosk -storepass android -keypass android \
    -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=poctamat"
fi

echo "== 8. sign =="
"$BT/apksigner.bat" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
  --out "$OUT" "$BUILD/app-final.apk"

echo "== DONE =="
"$BT/apksigner.bat" verify --print-certs "$OUT" | head -3 || true
ls -la "$OUT"
