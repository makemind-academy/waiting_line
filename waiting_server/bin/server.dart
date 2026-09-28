import 'dart:async';
import 'dart:convert';

import 'package:mcp_server/mcp_server.dart';

import 'serve_bundle.dart';

/// waiting_server — the queue outside a restaurant.
///
/// Two people look at this queue from two places. Somebody at the door writes
/// a party into it; somebody at the counter calls the next one. They are
/// looking at the same thing and they must never disagree about it.
///
/// So there is exactly one list here, and both screens read it. Neither screen
/// keeps its own copy of "how many are waiting" — the moment a screen starts
/// remembering, the two screens can drift, and a drifted queue is worse than
/// no queue because now somebody is arguing with a customer.
///
/// The wait estimate is not stored either. It is computed from the parties
/// that are actually in the list, every time it is asked for. A stored estimate
/// is a number that was true once.
void main(List<String> args) async {
  const config = McpServerConfig(
    name: 'Waiting Line',
    version: '1.0.0',
    capabilities: ServerCapabilities(
      tools: ToolsCapability(listChanged: true),
      resources: ResourcesCapability(listChanged: true),
    ),
  );
  final server = McpServer.createServer(config);
  WaitingServer(server).register();
  // The screen next door: AppPlayer reads it from here and sends the pages'
  // tool calls back to the tools above.
  registerBundleUi(server, '../waiting.mbd');
  final transport = await McpServer.createTransport(transportFor(args)).get();
  server.connect(transport);
  await Completer<void>().future;
}

/// `--http=<port>` serves over streamable HTTP so that more than one client can
/// share this one process (a screen in AppPlayer and a second party on the
/// side). Without it the server speaks stdio, which is what a launcher expects.
TransportConfig transportFor(List<String> args) {
  final http = args.firstWhere((a) => a.startsWith('--http='), orElse: () => '');
  if (http.isEmpty) return const TransportConfig.stdio();
  return TransportConfig.streamableHttp(
    host: 'localhost',
    port: int.parse(http.substring('--http='.length)),
    endpoint: '/mcp',
  );
}

/// One party standing outside.

/// "1 order", "2 orders". A screen that says "1 orders" is a screen
/// nobody proofread.
String _plural(int n, String one) => "$n $one" + (n == 1 ? "" : "s");

class Party {
  Party(this.ticket, this.name, this.size, this.joinedAtMinute);

  final int ticket;
  final String name;
  final int size;

  /// Minutes since the shift began. Carried on the record, not read from a
  /// clock later — see [WaitingServer._now] for why.
  final int joinedAtMinute;

  String? calledAtNote;

  /// How long this party actually stood outside. Set when they are called,
  /// because until then it is not a fact yet.
  int waitedMinutes = 0;
}

class WaitingServer {
  WaitingServer(this.server);

  final Server server;

  /// Average minutes a table takes to turn over. The one number the owner
  /// would ever change, and it lives here alone.
  static const _minutesPerTable = 9;

  /// How long a called party's place is held. A queue's own rule, printed on
  /// both screens so nobody has to remember it.
  static const _holdMinutes = 10;

  /// The shift clock. A real installation reads the wall clock; this sample
  /// advances it explicitly so that the same run produces the same log, which
  /// is what makes the verification meaningful.
  int _now = 0;

  final _waiting = <Party>[];
  final _served = <Party>[];
  int _nextTicket = 41;

  static const _stateUri = 'line://state';

  void register() {
    // The queue as a resource: a screen subscribes once and is told when it
    // moves, so the door follows the counter without anybody touching it.
    server.addResource(
      uri: _stateUri,
      name: 'The line',
      description: 'Who is waiting, in order, with the wait estimate',
      mimeType: 'application/json',
      handler: (uri, params) async => ReadResourceResult(contents: [
        ResourceContentInfo(
            uri: _stateUri, mimeType: 'application/json', text: _stateJson()),
      ]),
    );

    server.addTool(
      name: 'line.state',
      description: 'Who is waiting, in order, with the wait estimate',
      inputSchema: const {'type': 'object', 'properties': {}},
      handler: (args) async => _state(),
    );

    server.addTool(
      name: 'line.add',
      description: 'Write a party into the queue',
      inputSchema: const {
        'type': 'object',
        'properties': {
          'name': {'type': 'string'},
          'size': {'type': 'integer'},
        },
        'required': ['name', 'size'],
      },
      handler: (args) async {
        final p = Party(_nextTicket++, args['name'] as String,
            args['size'] as int, _now);
        _waiting.add(p);
        server.notifyResourceUpdated(_stateUri);
        return _state(notice: 'ticket ${p.ticket} written at the door');
      },
    );

    server.addTool(
      name: 'line.call',
      description: 'Call the party at the front and seat them',
      inputSchema: const {'type': 'object', 'properties': {}},
      handler: (args) async {
        if (_waiting.isEmpty) return _state(notice: 'nobody is waiting');
        final p = _waiting.removeAt(0);
        // The party leaves the waiting list but not the record. An owner who
        // wants to know how long people actually waited has to be able to ask
        // afterwards, and a deleted row cannot answer.
        p.waitedMinutes = _now - p.joinedAtMinute;
        p.calledAtNote = 'waited ${p.waitedMinutes} min';
        _served.add(p);
        server.notifyResourceUpdated(_stateUri);
        return _state(notice: 'called ${p.name} (${p.ticket})');
      },
    );

    server.addTool(
      name: 'line.tick',
      description: 'Advance the shift clock by some minutes',
      inputSchema: const {
        'type': 'object',
        'properties': {
          'minutes': {'type': 'integer'},
        },
        'required': ['minutes'],
      },
      handler: (args) async {
        _now += args['minutes'] as int;
        server.notifyResourceUpdated(_stateUri);
        return _state(notice: 'shift clock at $_now min');
      },
    );
  }

  /// Everything both screens see. Computed, never cached.
  CallToolResult _state({String notice = ''}) => CallToolResult(content: [
        TextContent(text: _stateJson(notice: notice)),
      ]);

  String _stateJson({String notice = ''}) {
    final heads = _waiting.fold<int>(0, (a, p) => a + p.size);
    return jsonEncode({
          'rows': [
            for (var i = 0; i < _waiting.length; i++)
              {
                'ticket': '#${_waiting[i].ticket}',
                'name': _waiting[i].name,
                'party': '${_waiting[i].size} people',
                // Position is derived from the list order. Nothing stores
                // "you are third" — that would be wrong the moment somebody
                // ahead gives up and leaves.
                'ahead': i == 0 ? 'next' : '$i ahead',
                'waited': '${_now - _waiting[i].joinedAtMinute} min',
              },
          ],
          'parties': _waiting.length,
          'heads': _plural(heads, 'person').replaceFirst('persons', 'people'),
          // The estimate the person at the door reads out loud.
          'estimate': '${_waiting.length * _minutesPerTable} min',
          'served': _served.length,
          'servedLine': _served.isEmpty
              ? 'nobody seated yet'
              : _served
                  .map((p) => '${p.name} ${p.calledAtNote}')
                  .join(' · '),
          'clock': '$_now min into the shift',
          // What the counter's own screen needs: the party the button is
          // about, and the two figures a manager asks for at the end of a
          // shift. All three are derived here — a screen that computed its
          // own average would disagree with the one next to it.
          'nextLabel': _waiting.isEmpty
              ? 'nobody waiting'
              : '#${_waiting.first.ticket} ${_waiting.first.name} (${_waiting.first.size})',
          'avgWaitLabel': _served.isEmpty
              ? '-'
              : '${(_served.fold<int>(0, (a, p) => a + p.waitedMinutes) / _served.length).round()} min',
          // The rule a queue display has to print, because it is the part
          // people argue about. It lives with the queue, not on the screen.
          'holdRule': 'We hold your place for $_holdMinutes min after we call',
          'notice': notice,
        });
  }
}
