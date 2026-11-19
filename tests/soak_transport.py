"""Optional 75-second SDK connection check against the local fixture server."""

import asyncio
import json
import sys
import unittest

import test_transport


class Soak(test_transport.TransportTests):
    async def test_soak(self):
        await self.owner.initialize()
        await asyncio.wait_for(self.sent.wait(), timeout=2)
        session = self.created[0]._jupiter_transport.client.get_wrapped_session()
        initial = len(asyncio.all_tasks())
        for seconds in (15, 30, 45, 60, 75):
            await asyncio.sleep(15)
            self.assertFalse(session.closed)
            self.assertEqual(self.plugin.status[123], '已连接')
            self.assertEqual(len(self.created), 1)
            self.foreign.assert_not_called()
            tasks = len(asyncio.all_tasks())
            self.assertLessEqual(tasks, initial + 2)
            print(json.dumps({'elapsed': seconds, 'connected': True, 'tasks': tasks}), flush=True)
        await self.owner.terminate()
        self.assertTrue(session.closed)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=1).run(Soak('test_soak'))
    sys.exit(not result.wasSuccessful())
