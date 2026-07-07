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
        # Daftar user_id dalam antrian (urutan masuk → bisa cek posisi)
        self._regular_tracking: list[int] = []
        self._premium_tracking: list[int] = []

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

    def add_job(self, job, is_premium: bool = False, user_id: int = 0) -> int:
        """
        Tambah job ke antrian.
        Mengembalikan posisi 1-based dalam antrian (1 = giliran berikutnya).
        Mengembalikan 0 jika antrian penuh (gagal ditambahkan).
        """
        track = self._premium_tracking if is_premium else self._regular_tracking
        queue = self.premium_queue   if is_premium else self.regular_queue

        async def _tracked():
            # Hapus dari tracking saat worker mulai memproses job ini
            if user_id and user_id in track:
                track.remove(user_id)
            await job()

        try:
            # Enqueue dulu — kalau gagal (QueueFull), tracking tidak tersentuh
            queue.put_nowait(_tracked)
        except asyncio.QueueFull:
            return 0  # gagal, tidak ada yang perlu di-rollback

        # Append tracking hanya setelah enqueue berhasil
        if user_id:
            track.append(user_id)
        return len(track)  # posisi 1-based

    def get_position(self, user_id: int, is_premium: bool = False) -> int:
        """
        Kembalikan posisi 1-based user dalam antrian.
        0 = tidak ada job user ini dalam antrian.
        """
        track = self._premium_tracking if is_premium else self._regular_tracking
        try:
            return track.index(user_id) + 1
        except ValueError:
            return 0

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
