import asyncio
import unittest

from modules import lavalink


class LavalinkVoiceClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        client = lavalink.LavalinkVoiceClient.__new__(lavalink.LavalinkVoiceClient)
        client.guild = type("Guild", (), {"id": 123})()
        client.node = None
        client._after = None
        client._playing = False
        client._paused = False
        client._stop_requested = False
        client._volume = 100
        client.last_exception = None
        client._destroyed = False
        client._connected = True
        client._connect_evt = asyncio.Event()
        client._connect_evt.set()
        client.cleanup = lambda: None
        return client

    async def test_track_exception_immediately_advances_player(self):
        client = self.make_client()
        ended = []
        client._after = lambda error: ended.append(error)
        client._playing = True

        await client.on_lavalink_event({
            "type": "TrackExceptionEvent",
            "exception": {"message": "blocked"},
        })

        self.assertFalse(client._playing)
        self.assertEqual(client.last_exception, "blocked")
        self.assertEqual(len(ended), 1)

    async def test_stale_track_exception_does_not_end_new_track(self):
        client = self.make_client()
        ended = []
        client._playing = True
        client._encoded = "new-track"
        client._after = lambda error: ended.append(error)

        await client.on_lavalink_event({
            "type": "TrackExceptionEvent",
            "track": {"encoded": "old-track"},
            "exception": {"message": "late event"},
        })

        self.assertTrue(client._playing)
        self.assertEqual(ended, [])

    async def test_play_track_rest_failure_resets_state(self):
        client = self.make_client()
        ended = []
        client._after = lambda error: ended.append(error)

        class Node:
            async def update_player(self, guild_id, body, no_replace=False):
                raise lavalink.LavalinkError("node unavailable")

        client.node = Node()
        track = type("Track", (), {"encoded": "encoded"})()

        with self.assertRaises(lavalink.LavalinkError):
            await client.play_track(track, 50, lambda error: ended.append(error))

        self.assertFalse(client._playing)
        self.assertFalse(client._paused)
        self.assertEqual(len(ended), 1)

    async def test_repeated_teardown_is_noop(self):
        client = self.make_client()
        destroyed = []

        async def destroy_player(node, guild_id):
            destroyed.append(guild_id)

        client.node = type("Node", (), {
            "listeners": {123: client},
            "destroy_player": destroy_player,
        })()
        client._after = lambda error: None
        client._playing = True

        await client._teardown()
        await client._teardown()

        self.assertEqual(destroyed, [123])
        self.assertFalse(client._playing)

    async def test_stop_waits_for_lavalink_before_advancing(self):
        client = self.make_client()
        release_stop = asyncio.Event()
        ended = []

        class Node:
            async def update_player(self, guild_id, body, no_replace=False):
                await release_stop.wait()

        client.node = Node()
        client._playing = True
        client._encoded = "old-track"
        client._after = lambda error: ended.append(error)
        client.stop()
        await asyncio.sleep(0)

        await client.on_lavalink_event({
            "type": "TrackEndEvent",
            "reason": "stopped",
            "track": {"encoded": "old-track"},
        })
        self.assertEqual(ended, [])

        release_stop.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(ended, [None])
        self.assertFalse(client._playing)

        client._playing = True
        client._encoded = "new-track"
        client._after = lambda error: ended.append("new track ended")
        await client.on_lavalink_event({
            "type": "TrackEndEvent",
            "reason": "stopped",
            "track": {"encoded": "old-track"},
        })

        self.assertTrue(client._playing)
        self.assertEqual(ended, [None])


if __name__ == "__main__":
    unittest.main()
