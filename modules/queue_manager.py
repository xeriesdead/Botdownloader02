import asyncio
from logger import logger

PREMIUM_PRIORITY = 3
GLOBAL_DELAY     = 1.2
JOB_TIMEOUT      = 600   # 10 menit — cukup untuk file besar hingga 1 GB
WORKER_COUNT     = 3


class QueueManager:

    def __init__(self):
        self.regular_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.premium_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.running       = False
        self._active_count = 0

    async def _worker(self):
        premium_counter = 0
        while True:
            try:
                job = None

                # Coba ambil premium job dulu (prioritas)
                if premium_counter < PREMIUM_PRIORITY:
                    try:
                        job = self.premium_queue.get_nowait()
                        premium_counter += 1
                    except asyncio.QueueEmpty:
                        pass

                # Kalau tidak ada premium, ambil regular
                if job is None:
                    try:
                        job = self.regular_queue.get_nowait()
                        premium_counter = 0
                    except asyncio.QueueEmpty:
                        pass

                # Kalau keduanya kosong, tunggu sebentar lalu coba lagi
                # (jangan block di satu queue saja supaya dua queue tetap dicek)
                if job is None:
                    await asyncio.sleep(0.1)
                    continue

                await asyncio.sleep(GLOBAL_DELAY)

                self._active_count += 1
                try:
                    await asyncio.wait_for(job(), timeout=JOB_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("Job timeout setelah %ds", JOB_TIMEOUT)
                except Exception as e:
                    logger.error("Job error: %s", e, exc_info=True)
                finally:
                    self._active_count -= 1

            except Exception as e:
                logger.error("Queue worker error: %s", e, exc_info=True)

    async def start(self, worker_count: int = WORKER_COUNT):
        if self.running:
            return
        self.running = True
        for _ in range(worker_count):
            asyncio.create_task(self._worker())
        logger.info("QueueManager started (%d workers)", worker_count)

    def can_add(self, is_premium: bool = False) -> bool:
        """Cek apakah masih bisa menerima job baru tanpa benar-benar menambahkan."""
        if is_premium:
            return not self.premium_queue.full()
        return not self.regular_queue.full()

    def add_job(self, job, is_premium: bool = False) -> bool:
        try:
            if is_premium:
                self.premium_queue.put_nowait(job)
            else:
                self.regular_queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            return False

    @property
    def active(self) -> int:
        return self._active_count

    @property
    def size(self) -> dict:
        return {
            "regular": self.regular_queue.qsize(),
            "premium": self.premium_queue.qsize(),
        }


queue_manager = QueueManager()
