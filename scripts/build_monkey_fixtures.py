#!/usr/bin/env python3
"""Build public, disposable positive/crash/ANR controls; no app source or secrets."""
import argparse
from pathlib import Path
import subprocess
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True)
    tools = args.sdk / 'build-tools/35.0.0'
    android = args.sdk / 'platforms/android-30/android.jar'
    key = args.out / 'fixture-only.keystore'
    subprocess.run(['keytool', '-genkeypair', '-keystore', str(key), '-storepass', 'fixture-only', '-keypass', 'fixture-only',
                    '-alias', 'fixture', '-dname', 'CN=Disposable Monkey Control', '-keyalg', 'RSA', '-validity', '2'], check=True)
    bodies = {'healthy': 'button.setText("Still running");',
              'crash': 'throw new RuntimeException("intentional-public-monkey-control");',
              'anr': 'try { Thread.sleep(60000); } catch (InterruptedException ignored) { }'}
    for mode, body in bodies.items():
        work = args.out / mode
        work.mkdir()
        classes = work / 'classes'
        classes.mkdir()
        java = work / 'MainActivity.java'
        java.write_text('''package app.aaps;
public class MainActivity extends android.app.Activity {
 public void onCreate(android.os.Bundle saved) {
  super.onCreate(saved);
  final android.widget.Button button = new android.widget.Button(this);
  button.setText("Public Monkey control");
  button.setOnClickListener(new android.view.View.OnClickListener() {
   public void onClick(android.view.View view) { BODY }
  });
  setContentView(button);
 }
}'''.replace('BODY', body))
        manifest = work / 'AndroidManifest.xml'
        manifest.write_text('''<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="info.nightscout.androidaps" android:versionCode="1" android:versionName="fixture">
<uses-sdk android:minSdkVersion="27" android:targetSdkVersion="30"/>
<application android:label="Disposable Monkey Control" android:debuggable="false" android:theme="@android:style/Theme.Material.Light.NoActionBar">
<activity android:name="app.aaps.MainActivity" android:exported="true"><intent-filter><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity>
</application></manifest>''')
        subprocess.run(['javac', '--release', '8', '-cp', str(android), '-d', str(classes), str(java)], check=True)
        jar = work / 'classes.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            for path in classes.rglob('*.class'):
                archive.write(path, path.relative_to(classes))
        dex = work / 'dex'
        dex.mkdir()
        subprocess.run([str(tools / 'd8'), '--min-api', '27', '--lib', str(android), '--output', str(dex), str(jar)], check=True)
        base = work / 'base.apk'
        subprocess.run([str(tools / 'aapt2'), 'link', '-I', str(android), '--manifest', str(manifest), '-o', str(base)], check=True)
        with zipfile.ZipFile(base, 'a') as archive:
            archive.write(dex / 'classes.dex', 'classes.dex')
        aligned = work / 'aligned.apk'
        subprocess.run([str(tools / 'zipalign'), '-f', '4', str(base), str(aligned)], check=True)
        subprocess.run([str(tools / 'apksigner'), 'sign', '--ks', str(key), '--ks-pass', 'pass:fixture-only',
                        '--key-pass', 'pass:fixture-only', '--out', str(args.out / (mode + '.apk')), str(aligned)], check=True)


if __name__ == '__main__':
    main()
