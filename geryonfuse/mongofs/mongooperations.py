import collections
import configparser
import datetime
import errno
import fuse
import io
import math
import pymongo
import re
import stat
import time
from typing import Dict, Any
from geryonfuse.logger.geryon_logger import build_logger
from geryonfuse.mongofs.mongo_connector import MongoConnector

READDIR_RESULT_ITEM = collections.namedtuple("READDIR_RESULT_ITEM", ["filename", "attrs", "offset"])


class MongoOperations(fuse.Operations):
    CONFIG_SECTION = "mongofs"
    DEFAULT_COLLECTION_NAME = "mongofs-drive"

    NOT_IMPLEMENT_EXIT_CODE = 0
    SUCCESS_EXIT_CODE = 0
    ERROR_EXIT_CODE = 1

    def __new__(cls, config: configparser.ConfigParser):
        cls.__config = config
        cls.__logger = build_logger(config=cls.__config)
        return super(MongoOperations, cls).__new__(cls)

    def __init__(self, config: configparser.ConfigParser) -> None:
        mongo_connector = MongoConnector(config=config)
        self.col = mongo_connector.connect()
        if not self.col.find_one({"path": "/"}):
            self.create_root()

    @classmethod
    def __parent_path(cls, path: str) -> str:
        last_slash_pos = path.rfind("/")
        if last_slash_pos == 0:
            return "/"
        return path[:last_slash_pos]

    @classmethod
    def __build_doc(cls, path: str, doc_type: str, content: bytes = None) -> Dict[str, Any]:
        now = datetime.datetime.utcnow()
        doc = {"path": path, "type": doc_type, "created_at": now, "last_updated_at": now}
        if content is not None:
            doc["content"] = content
            doc["size"] = len(content)
        return doc

    def __create_doc(self, path: str, doc_type: str, content: bytes = None) -> Dict[str, Any]:
        doc = self.__build_doc(path=path, doc_type=doc_type, content=content)
        self.col.insert_one(doc)
        return doc

    @classmethod
    def __now(cls) -> datetime.datetime:
        return datetime.datetime.utcnow()

    def __create_file(self, path: str, content: bytes = b"") -> Dict[str, Any]:
        return self.__create_doc(path=path, doc_type="file", content=content)

    def __create_dir(self, path: str) -> Dict[str, Any]:
        return self.__create_doc(path=path, doc_type="dir")

    def __build_stat_from_doc(self, doc):
        (uid, gid, pid) = fuse.fuse_get_context()

        block_size = 1_000_000  # 1 MB

        if doc["type"] == "dir":
            permission = 0o777
        else:
            permission = 0o666

        last_updated_at_epoch = doc["last_updated_at"].timestamp()
        stat_result = {
            "st_mtime": last_updated_at_epoch,  # modified time.
            "st_ctime": last_updated_at_epoch,  # changed time.
            "st_atime": time.time(),
            "st_uid": uid,
            "st_gid": gid,
        }

        if doc["type"] == "dir":
            stat_result["st_size"] = 1024 * 4  # Size of a directory, 4KB per some sources?
            stat_result["st_mode"] = (stat.S_IFDIR | permission)
            stat_result["st_nlink"] = 2  # Number of hard links to a directory, 2 because of historical reasons
        else:
            self.__logger.debug(doc)
            stat_result["st_size"] = doc.get("size", 0)  # TODO: can size not exists?
            stat_result["st_mode"] = (stat.S_IFREG | permission)
            stat_result["st_nlink"] = 1
            self.__logger.debug(f"doc is ok: {doc}")

        stat_result["st_blocks"] = int(math.ceil(float(stat_result["st_size"]) / block_size))

        self.__logger.debug(f"stat_result {stat_result}")
        return stat_result

    def chmod(self, path, mode):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def chown(self, path, uid, gid):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def getattr(self, path, fh=None):
        self.__logger.debug(f"getattr of {path} {fh}")
        doc = self.col.find_one({"path": path})
        self.__logger.debug(f"doc /: {doc}")
        if doc is None:
            self.__logger.debug(f"doc DOESN'T exist in {path} before __build_stat_from_doc")
            raise fuse.FuseOSError(errno.ENOENT)

        self.__logger.debug(f"doc exists in {path} before __build_stat_from_doc")
        return self.__build_stat_from_doc(doc=doc)

    def readdir(self, path, offset=0):
        self.__logger.debug(f"readdir {path}")
        yield "."
        yield ".."
        if path == "/":
            self.__logger.debug(f"path /: {path}")
            file_docs = self.col.find({"path": {"$regex": "^/[^/]+$"}}).sort([("path", pymongo.ASCENDING)])
            #self.__logger.debug(f"file_docs in /: {list(file_docs)}")
        else:
            self.__logger.debug(f"path std: {path}")
            file_docs = self.col.find({"path": {"$regex": f"^{path}/[^/]+$"}}).sort([("path", pymongo.ASCENDING)])
            #self.__logger.debug(f"file_docs in {path}: {list(file_docs)}")

        for file_doc in file_docs:
            file_name = re.sub(f"^{path}/?", "", file_doc["path"])
            self.__logger.debug(f"file_name is {file_name}")
            yield file_name

    def readlink(self, path):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def mknod(self, path, mode, dev):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def rmdir(self, path):
        self.col.delete_one({"path": path})
        self.col.delete_many({"path": {"$regex": f"{path}/*"}})
        return self.SUCCESS_EXIT_CODE

    def mkdir(self, path, mode="r"):
        self.__logger.debug(f"mkdir {path}")
        parent_path = self.__parent_path(path=path)
        if self.col.find_one({"path": parent_path, "type": "dir"}):
            self.__logger.debug(f"parent path {path} exists")
            self.__create_dir(path=path)
            return 0
        self.__logger.debug(f"parent path {path} does not exist")
        raise fuse.FuseOSError(errno.EIO)

    def statfs(self, path):
        self.__logger.debug(f"statfs {path}")
        # TODO: check this stats
        transfer_block_size = 256
        fragment_size = 256
        total_data_blocks_in_fs = 200_000
        free_blocks_in_fs = 200_000
        return {
            "f_bsize": transfer_block_size,
            "f_blocks": total_data_blocks_in_fs,
            "f_frsize": fragment_size,
            "f_bfree": free_blocks_in_fs,
            "f_bavail": free_blocks_in_fs
        }

    def unlink(self, path):
        self.__logger.debug(f"unlink {path}")
        file_doc = self.col.find_one({"path": path})
        if file_doc:
            if file_doc["type"] == "dir":
                raise fuse.FuseOSError(errno.EISDIR)
            res = self.col.delete_one({"path": path})
            return self.SUCCESS_EXIT_CODE if res else self.ERROR_EXIT_CODE
        else:
            raise fuse.FuseOSError(errno.ENOENT)

    def symlink(self, name, target):
        return self.SUCCESS_EXIT_CODE

    def rename(self, old, new):
        self.__logger.debug(f"rename {old} to {new}")
        res = self.col.update_one({"path": old}, {"$set": {"path": new, "last_updated_at": self.__now()}})
        if res is None:
            raise fuse.FuseOSError(errno.ENOENT)

    def link(self, target, name):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def utimens(self, path, times=None):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def open(self, path, flags=None):
        self.__logger.debug(f"open {path}")
        file_doc = self.col.find_one({"path": path})
        self.__logger.debug(f"file_doc is {file_doc}")
        if file_doc is None:
            raise fuse.FuseOSError(errno.EACCES)
        return self.SUCCESS_EXIT_CODE

    def create(self, path, mode=None, fi=None):
        self.__logger.debug(f"create {path}")
        parent_path = self.__parent_path(path=path)
        if self.col.find_one({"path": parent_path, "type": "dir"}):
            self.__logger.debug(f"parent path {path} exists")
            self.__create_file(path=path)
            return 0
        self.__logger.debug(f"parent path {path} does not exist")
        raise fuse.FuseOSError(errno.EIO)

    def read(self, path, length, offset=0, fh=None):
        self.__logger.debug(f"read {path}")
        file_doc = self.col.find_one({"path": path})
        if file_doc is None:
            raise fuse.FuseOSError(errno.EIO)
        # TODO: decrypt content
        file_io = io.BytesIO(file_doc["content"])
        file_io.seek(offset)
        return file_io.read(length)

    def write(self, path, buf, offset=0, fh=None):
        self.__logger.debug(f"write {path}")
        now = datetime.datetime.utcnow()
        file_doc = self.col.find_one({"path": path})
        if file_doc is None:
            raise fuse.FuseOSError(errno.EIO)
        # TODO: decrypt content
        file_io = io.BytesIO(file_doc["content"])
        file_io.seek(offset)
        file_io.write(buf)
        file_io.seek(0)
        # TODO: encrypt content
        updated_content = file_io.read()
        res = self.col.update_one(filter={"path": path},
                                  update={"$set": {"content": updated_content, "size": len(buf),
                                                   "last_updated_at": self.__now()}})
        if res:
            self.__logger.debug(f"write {path} was OK")
            return len(buf)
        raise fuse.FuseOSError(errno.EIO)

    def truncate(self, path, length=None, fh=None):
        self.__logger.debug(f"truncate {path}")
        res = self.col.update_one({"path": path},
                                  {"$set": {"content": b"", "size": 0, "last_updated_at": self.__now()}})
        return self.SUCCESS_EXIT_CODE if res else self.ERROR_EXIT_CODE

    def flush(self, path, fh):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def release(self, path, fh):
        return self.NOT_IMPLEMENT_EXIT_CODE

    def fsync(self, path, fdatasync, fh):
        return self.NOT_IMPLEMENT_EXIT_CODE

    # Useful method
    # TODO: move to future MongoConnection class?
    def wipe(self):
        self.col.delete_many(filter={})
        self.create_root()

    def create_root(self):
        self.__create_dir(path="/")
