// serve_bundle.dart — let an MCP server serve the screen that sits next to it.
//
// A sample keeps its screen in a `.mbd` folder (manifest.json + ui/app.json +
// ui/pages/*.json) and its data in an mcp_server. AppPlayer opens the server,
// reads `ui://app`, follows the routes to `ui://pages/<name>`, and sends the
// pages' tool actions back to the same server. This file is the forty lines
// that make a server do the serving half. Copy it next to `server.dart`; it has
// no dependency beyond mcp_server itself.
//
// The files are read on every request, so editing a page and reopening the app
// shows the edit — nothing is built.

import 'dart:convert';
import 'dart:io';

import 'package:mcp_server/mcp_server.dart';

/// Registers `ui://app`, `ui://app/info` and one `ui://pages/<name>` per file
/// under `<bundleDir>/ui/pages/`.
void registerBundleUi(Server server, String bundleDir) {
  final dir = Directory(bundleDir);
  if (!dir.existsSync()) {
    throw ArgumentError('no bundle folder at ${dir.absolute.path}');
  }
  final manifest = _json('$bundleDir/manifest.json')['manifest'] as Map<String, dynamic>;

  void serve(String uri, String name, String description,
      Map<String, dynamic> Function() document) {
    server.addResource(
      uri: uri,
      name: name,
      description: description,
      mimeType: 'application/json',
      handler: (requestedUri, params) async => ReadResourceResult(
        contents: [
          ResourceContentInfo(
            uri: requestedUri,
            mimeType: 'application/json',
            text: jsonEncode(document()),
          ),
        ],
      ),
    );
  }

  serve('ui://app', manifest['name'] as String, 'The app: routes and theme',
      () => _json('$bundleDir/ui/app.json'));

  // §11.6 — what a launcher shows before loading anything.
  serve('ui://app/info', 'App Info', 'Application metadata (spec §11.6)', () => {
        'id': manifest['id'],
        'name': manifest['name'],
        'version': manifest['version'],
        'description': manifest['description'],
        if (manifest['category'] != null) 'category': manifest['category'],
        if (manifest['publisher'] != null) 'publisher': manifest['publisher'],
        if (manifest['tags'] != null) 'tags': manifest['tags'],
      });

  for (final f in Directory('$bundleDir/ui/pages').listSync().whereType<File>()) {
    if (!f.path.endsWith('.json')) continue;
    final name = f.uri.pathSegments.last.replaceAll('.json', '');
    serve('ui://pages/$name', name, 'Screen "$name" from the bundle folder',
        () => _json(f.path));
  }
}

Map<String, dynamic> _json(String path) =>
    jsonDecode(File(path).readAsStringSync()) as Map<String, dynamic>;
