import json
from copy import deepcopy
from hashlib import sha256
from operator import itemgetter
from typing import Iterable, Any, List

import pyorient

from ledger.compact_merkle_tree import CompactMerkleTree
from ledger.serializers.compact_serializer import CompactSerializer
from ledger.stores.file_hash_store import FileHashStore
from ledger.util import F
from plenum.common.exceptions import InvalidClientRequest, \
    UnauthorizedClientRequest
from plenum.common.log import getlogger
from plenum.common.txn import NAME, VERSION, ORIGIN, \
    POOL_TXN_TYPES, VERKEY
from plenum.common.types import Reply, RequestAck, RequestNack, f, \
    NODE_PRIMARY_STORAGE_SUFFIX, OPERATION, LedgerStatus, DOMAIN_LEDGER_ID
from plenum.common.util import error
from plenum.persistence.storage import initStorage
from plenum.server.node import Node as PlenumNode
from plenum.common.ledger import Ledger

from sovrin_common.config_util import getConfig
from sovrin_common.txn import TXN_TYPE, \
    TARGET_NYM, allOpKeys, ATTRIB, NYM,\
    ROLE, GET_ATTR, DISCLO, DATA, GET_NYM, \
    TXN_ID, reqOpKeys, GET_TXNS, LAST_TXN, TXNS, \
    getTxnOrderedFields, SCHEMA, GET_SCHEMA,\
    ISSUER_KEY, GET_ISSUER_KEY, REF, TRUSTEE, TGB, POOL_UPGRADE, ACTION, \
    NODE_UPGRADE, COMPLETE, FAIL, HASH, ENC, RAW
from sovrin_common.types import Request
from sovrin_common.util import dateTimeEncoding
from sovrin_common.persistence import identity_graph
from sovrin_node.persistence.secondary_storage import SecondaryStorage
from sovrin_common.auth import Authoriser
from sovrin_node.server.client_authn import TxnBasedAuthNr
from sovrin_node.server.config_req_handler import ConfigReqHandler
from sovrin_node.server.constants import CONFIG_LEDGER_ID, openTxns, \
    validTxnTypes, IDENTITY_TXN_TYPES, CONFIG_TXN_TYPES
from sovrin_node.server.domain_req_handler import DomainReqHandler
from sovrin_node.server.node_authn import NodeAuthNr
from sovrin_node.server.pool_manager import HasPoolManager
from sovrin_node.server.upgrader import Upgrader
from plenum.common.types import POOL_LEDGER_ID
from sovrin_node.persistence.state_tree_store import StateTreeStore

logger = getlogger()


class Node(PlenumNode, HasPoolManager):
    keygenScript = "init_sovrin_raet_keep"

    def __init__(self,
                 name,
                 nodeRegistry=None,
                 clientAuthNr=None,
                 ha=None,
                 cliname=None,
                 cliha=None,
                 basedirpath=None,
                 primaryDecider=None,
                 pluginPaths: Iterable[str] = None,
                 storage=None,
                 config=None):
        self.config = config or getConfig()
        # self.graphStore = self.getGraphStorage(name)
        super().__init__(name=name,
                         nodeRegistry=nodeRegistry,
                         clientAuthNr=clientAuthNr,
                         ha=ha,
                         cliname=cliname,
                         cliha=cliha,
                         basedirpath=basedirpath,
                         primaryDecider=primaryDecider,
                         pluginPaths=pluginPaths,
                         storage=storage,
                         config=self.config)
        domainState = self.getState(DOMAIN_LEDGER_ID)
        self.stateTreeStore = StateTreeStore(domainState)
        # self._addTxnsToGraphIfNeeded()
        self.configLedger = self.getConfigLedger()
        self.ledgerManager.addLedger(CONFIG_LEDGER_ID, self.configLedger,
                                     postCatchupCompleteClbk=self.postConfigLedgerCaughtUp,
                                     postTxnAddedToLedgerClbk=self.postTxnFromCatchupAddedToLedger)
        self.upgrader = self.getUpgrader()
        self.nodeMsgRouter.routes[Request] = self.processNodeRequest
        self.nodeAuthNr = self.defaultNodeAuthNr()

    def initPoolManager(self, nodeRegistry, ha, cliname, cliha):
        HasPoolManager.__init__(self, nodeRegistry, ha, cliname, cliha)

    # def getSecondaryStorage(self):
    #     return SecondaryStorage(self.graphStore, self.primaryStorage)

    # def getGraphStorage(self, name):
    #     return identity_graph.IdentityGraph(self._getOrientDbStore(name,
    #                                                 pyorient.DB_TYPE_GRAPH))

    def getPrimaryStorage(self):
        """
        This is usually an implementation of Ledger
        """
        if self.config.primaryStorage is None:
            fields = getTxnOrderedFields()
            return Ledger(CompactMerkleTree(hashStore=self.hashStore),
                          dataDir=self.dataLocation,
                          serializer=CompactSerializer(fields=fields),
                          fileName=self.config.domainTransactionsFile,
                          ensureDurability=self.config.EnsureLedgerDurability)
        else:
            return initStorage(self.config.primaryStorage,
                               name=self.name + NODE_PRIMARY_STORAGE_SUFFIX,
                               dataDir=self.dataLocation,
                               config=self.config)

    def getUpgrader(self):
        return Upgrader(self.id,
                        self.name,
                        self.dataLocation,
                        self.config,
                        self.configLedger,
                        upgradeFailedCallback=self.postConfigLedgerCaughtUp)

    def getConfigLedger(self):
        return Ledger(CompactMerkleTree(hashStore=FileHashStore(
            fileNamePrefix='config', dataDir=self.dataLocation)),
            dataDir=self.dataLocation,
            fileName=self.config.configTransactionsFile,
            ensureDurability=self.config.EnsureLedgerDurability)

    def postDomainLedgerCaughtUp(self):
        # TODO: Reconsider, shouldn't config ledger be synced before domain
        # ledger, since processing config ledger can lead to restart and
        # thus rework (running the sync for leders again).
        # A counter argument is since domain ledger contains identities and thus
        # trustees, its needs to sync first
        super().postDomainLedgerCaughtUp()
        self.ledgerManager.setLedgerCanSync(CONFIG_LEDGER_ID, True)
        # Node has discovered other nodes now sync up domain ledger
        for nm in self.nodestack.connecteds:
            self.sendConfigLedgerStatus(nm)
        self.ledgerManager.processStashedLedgerStatuses(CONFIG_LEDGER_ID)

    def sendConfigLedgerStatus(self, nodeName):
        self.sendLedgerStatus(nodeName, CONFIG_LEDGER_ID)

    @property
    def configLedgerStatus(self):
        return LedgerStatus(CONFIG_LEDGER_ID, self.configLedger.size,
                            self.configLedger.root_hash)

    def getLedgerStatus(self, ledgerId: int):
        if ledgerId == CONFIG_LEDGER_ID:
            return self.configLedgerStatus
        else:
            return super().getLedgerStatus(ledgerId)

    def postPoolLedgerCaughtUp(self):
        # The only reason to override this is to set the correct node id in
        # the upgrader since when the upgrader is initialized, node might not
        # have its id since it maybe missing the complete pool ledger.
        # TODO: Maybe a cleaner way is to initialize upgrader only when pool
        # ledger has caught up.
        super().postPoolLedgerCaughtUp()
        self.upgrader.nodeId = self.id

    def postConfigLedgerCaughtUp(self):
        self.upgrader.processLedger()
        if self.upgrader.isItFirstRunAfterUpgrade:
            lastUpgradeVersion = self.upgrader.lastExecutedUpgradeInfo[1]
            op = {
                TXN_TYPE: NODE_UPGRADE,
                DATA: {
                    ACTION: None,
                    VERSION: lastUpgradeVersion
                }
            }

            op[DATA][ACTION] = COMPLETE if self.upgrader.didLastExecutedUpgradeSucceeded else FAIL
            op[f.SIG.nm] = self.wallet.signMsg(op[DATA])
            request = self.wallet.signOp(op)
            self.startedProcessingReq(*request.key, self.nodestack.name)
            self.send(request)

        # TODO:
        # if node finds code has not been upgraded where it should have been
            # (there is some configurable max time after scheduled time) then
            # it sends NODE_UPGRADE with status fail

    def processNodeRequest(self, request: Request, frm: str):
        if request.operation[TXN_TYPE] == NODE_UPGRADE:
            try:
                self.nodeAuthNr.authenticate(request.operation[DATA],
                                         request.identifier,
                                         request.operation[f.SIG.nm])
            except:
                # TODO: Do something here
                return
        if not self.isProcessingReq(*request.key):
            self.startedProcessingReq(*request.key, frm)
        # If not already got the propagate request(PROPAGATE) for the
        # corresponding client request(REQUEST)
        self.recordAndPropagate(request, frm)

    def postTxnFromCatchupAddedToLedger(self, ledgerId: int, txn: Any):
        if ledgerId == CONFIG_LEDGER_ID:
            pass
        else:
            super().postTxnFromCatchupAddedToLedger(ledgerId, txn)

    def validateNodeMsg(self, wrappedMsg):
        msg, frm = wrappedMsg
        if all(attr in msg.keys()
               for attr in [OPERATION, f.IDENTIFIER.nm, f.REQ_ID.nm]) \
                and msg.get(OPERATION, {}).get(TXN_TYPE) == NODE_UPGRADE:
            cls = Request
            cMsg = cls(**msg)
            return cMsg, frm
        else:
            return super().validateNodeMsg(wrappedMsg)

    def authNr(self, req):
        # TODO: Assumption that NODE_UPGRADE can be sent by nodes only
        if req.get(OPERATION, {}).get(TXN_TYPE) == NODE_UPGRADE:
            return self.nodeAuthNr
        else:
            return super().authNr(req)

    def _addTxnsToGraphIfNeeded(self):
        # TODO: should it be replaced by state tree store somehow?
        i = 0
        txnCountInGraph = self.graphStore.countTxns()
        for seqNo, txn in self.domainLedger.getAllTxn().items():
            if seqNo > txnCountInGraph:
                txn[F.seqNo.name] = seqNo
                self.storeTxnInGraph(txn)
                i += 1
        logger.debug("{} adding {} transactions to graph from ledger".
                     format(self, i))
        return i

    def isSignatureVerificationNeeded(self, msg: Any):
        op = msg.get(OPERATION)
        if op:
            if op.get(TXN_TYPE) in openTxns:
                return False
        return True

    def getDomainReqHandler(self):
        return DomainReqHandler(self.domainLedger,
                                self.states[DOMAIN_LEDGER_ID],
                                self.reqProcessors)

    def getConfigReqHandler(self):
        return ConfigReqHandler(self.configLedger,
                                self.states[CONFIG_LEDGER_ID])

    def doStaticValidation(self, identifier, reqId, operation):
        super().doStaticValidation(identifier, reqId, operation)
        unknownKeys = set(operation.keys()).difference(set(allOpKeys))
        if unknownKeys:
            raise InvalidClientRequest(identifier, reqId,
                                       'invalid keys "{}"'.
                                       format(",".join(unknownKeys)))

        missingKeys = set(reqOpKeys).difference(set(operation.keys()))
        if missingKeys:
            raise InvalidClientRequest(identifier, reqId,
                                       'missing required keys "{}"'.
                                       format(",".join(missingKeys)))

        if operation[TXN_TYPE] not in validTxnTypes:
            raise InvalidClientRequest(identifier, reqId, 'invalid {}: {}'.
                                       format(TXN_TYPE, operation[TXN_TYPE]))

        typ = operation.get(TXN_TYPE)
        ledgerId = self.ledgerId(typ)
        if ledgerId == DOMAIN_LEDGER_ID:
            self.reqHandler.doStaticValidation(identifier, reqId, operation)
        if ledgerId == CONFIG_LEDGER_ID:
            self.reqHandler.doStaticValidation(identifier, reqId, operation)

    # def checkValidOperation(self, identifier, reqId, operation):
    #     self.checkValidSovrinOperation(identifier, reqId, operation)
    #     super().checkValidOperation(identifier, reqId, operation)
    #
    # def checkValidSovrinOperation(self, identifier, reqId, operation):
    #     unknownKeys = set(operation.keys()).difference(set(allOpKeys))
    #     if unknownKeys:
    #         raise InvalidClientRequest(identifier, reqId,
    #                                    'invalid keys "{}"'.
    #                                    format(",".join(unknownKeys)))
    #
    #     missingKeys = set(reqOpKeys).difference(set(operation.keys()))
    #     if missingKeys:
    #         raise InvalidClientRequest(identifier, reqId,
    #                                    'missing required keys "{}"'.
    #                                    format(",".join(missingKeys)))
    #
    #     if operation[TXN_TYPE] not in validTxnTypes:
    #         raise InvalidClientRequest(identifier, reqId, 'invalid {}: {}'.
    #                                    format(TXN_TYPE, operation[TXN_TYPE]))
    #
    #     if operation[TXN_TYPE] == ATTRIB:
    #         dataKeys = {RAW, ENC, HASH}.intersection(set(operation.keys()))
    #         if len(dataKeys) != 1:
    #             raise InvalidClientRequest(identifier, reqId,
    #                                        '{} should have one and only one of '
    #                                        '{}, {}, {}'
    #                                        .format(ATTRIB, RAW, ENC, HASH))
    #         if RAW in dataKeys:
    #             try:
    #                 json.loads(operation[RAW])
    #             except:
    #                 raise InvalidClientRequest(identifier, reqId,
    #                                            'raw attribute {} should be '
    #                                            'JSON'.format(operation[RAW]))
    #
    #         if not (not operation.get(TARGET_NYM) or
    #                 self.graphStore.hasNym(operation[TARGET_NYM])):
    #             raise InvalidClientRequest(identifier, reqId,
    #                                        '{} should be added before adding '
    #                                        'attribute for it'.
    #                                        format(TARGET_NYM))
    #
    #     if operation[TXN_TYPE] == NYM:
    #         role = operation.get(ROLE)
    #         nym = operation.get(TARGET_NYM)
    #         if not nym:
    #             raise InvalidClientRequest(identifier, reqId,
    #                                        "{} needs to be present".
    #                                        format(TARGET_NYM))
    #         if not Authoriser.isValidRole(role):
    #             raise InvalidClientRequest(identifier, reqId,
    #                                        "{} not a valid role".
    #                                        format(role))
    #         # Only
    #         if not self.canNymRequestBeProcessed(identifier, operation):
    #             raise InvalidClientRequest(identifier, reqId,
    #                                        "{} is already present".
    #                                        format(nym))
    #
    #     if operation[TXN_TYPE] == POOL_UPGRADE:
    #         action = operation.get(ACTION)
    #         if action not in (START, CANCEL):
    #             raise InvalidClientRequest(identifier, reqId,
    #                                        "{} not a valid action".
    #                                        format(action))
    #         if action == START:
    #             schedule = operation.get(SCHEDULE, {})
    #             isValid, msg = self.upgrader.isScheduleValid(schedule,
    #                                                          self.poolManager.nodeIds)
    #             if not isValid:
    #                 raise InvalidClientRequest(identifier, reqId,
    #                                            "{} not a valid schedule since {}".
    #                                            format(schedule, msg))
    #
    #         # TODO: Check if cancel is submitted before start

    def checkRequestAuthorized(self, request: Request):
        op = request.operation
        typ = op[TXN_TYPE]

        s = self.graphStore  # type: identity_graph.IdentityGraph

        origin = request.identifier

        if typ == NYM:
            try:
                originRole = s.getRole(origin)
            except:
                raise UnauthorizedClientRequest(
                    request.identifier,
                    request.reqId,
                    "Nym {} not added to the ledger yet".format(origin))

            nymV = self.graphStore.getNym(op[TARGET_NYM])
            if not nymV:
                # If nym does not exist
                role = op.get(ROLE)
                r, msg = Authoriser.authorised(NYM, ROLE, originRole,
                                               oldVal=None, newVal=role)
                if not r:
                    raise UnauthorizedClientRequest(
                        request.identifier,
                        request.reqId,
                        "{} cannot add {}".format(originRole, role))
            else:
                nymData = nymV.oRecordData
                owner = self.graphStore.getOwnerFor(nymData.get(NYM))
                isOwner = origin == owner
                updateKeys = [ROLE, VERKEY]
                for key in updateKeys:
                    if key in op:
                        newVal = op[key]
                        oldVal = nymData.get(key)
                        if oldVal != newVal:
                            r, msg = Authoriser.authorised(NYM, key, originRole,
                                                           oldVal=oldVal,
                                                           newVal=newVal,
                                                           isActorOwnerOfSubject=isOwner)
                            if not r:
                                raise UnauthorizedClientRequest(
                                    request.identifier,
                                    request.reqId,
                                    "{} cannot update {}".format(originRole, key))

        elif typ == ATTRIB:
            if op.get(TARGET_NYM) and \
                op[TARGET_NYM] != request.identifier and \
                    not s.getOwnerFor(op[TARGET_NYM]) == origin:

                raise UnauthorizedClientRequest(
                        request.identifier,
                        request.reqId,
                        "Only identity owner/guardian can add attribute "
                        "for that identity")

        # TODO: Just for now. Later do something meaningful here
        elif typ in [DISCLO, GET_ATTR, SCHEMA, GET_SCHEMA, ISSUER_KEY,
                     GET_ISSUER_KEY]:
            pass
        elif request.operation.get(TXN_TYPE) in POOL_TXN_TYPES:
            return self.poolManager.checkRequestAuthorized(request)

        elif typ == POOL_UPGRADE:
            # TODO: Refactor urgently
            try:
                originRole = s.getRole(origin)
            except:
                raise UnauthorizedClientRequest(
                    request.identifier,
                    request.reqId,
                    "Nym {} not added to the ledger yet".format(origin))

            action = request.operation.get(ACTION)
            # TODO: Some validation needed for making sure name and version
            # present
            status = self.upgrader.statusInLedger(request.operation.get(NAME),
                                                  request.operation.get(VERSION))

            r, msg = Authoriser.authorised(POOL_UPGRADE, ACTION, originRole,
                                           oldVal=status, newVal=action)
            if not r:
                raise UnauthorizedClientRequest(
                    request.identifier,
                    request.reqId,
                    "{} cannot do {}".format(originRole, POOL_UPGRADE))

    def canNymRequestBeProcessed(self, identifier, msg):
        nym = msg.get(TARGET_NYM)
        if self.graphStore.hasNym(nym):
            if not self.graphStore.hasTrustee(identifier) and \
                            self.graphStore.getOwnerFor(nym) != identifier:
                    return False
        return True

    def defaultAuthNr(self):
        return TxnBasedAuthNr(self.graphStore)

    def defaultNodeAuthNr(self):
        return NodeAuthNr(self.poolLedger)

    async def prod(self, limit: int = None) -> int:
        c = await super().prod(limit)
        c += self.upgrader.service()
        return c

    def processGetNymReq(self, request: Request, frm: str):
        self.transmitToClient(RequestAck(*request.key), frm)
        nym = request.operation[TARGET_NYM]
        txn = self.graphStore.getAddNymTxn(nym)
        txnId = self.genTxnId(request.identifier, request.reqId)
        # TODO: We should have a single JSON encoder which does the
        # encoding for us, like sorting by keys, handling datetime objects.
        result = {f.IDENTIFIER.nm: request.identifier,
                  f.REQ_ID.nm: request.reqId,
                  DATA: json.dumps(txn, sort_keys=True) if txn else None,
                  TXN_ID: txnId
                  }
        result.update(request.operation)
        self.transmitToClient(Reply(result), frm)

    # TODO: Fix it later to retrive any txns with given seqNos,
    # chunked ledger will be used.
    # def processGetTxnReq(self, request: Request, frm: str):
    #     nym = request.operation[TARGET_NYM]
    #     origin = request.identifier
    #     if nym != origin:
    #         # TODO not sure this is correct; why does it matter?
    #         msg = "You can only receive transactions for yourself"
    #         self.transmitToClient(RequestNack(*request.key, msg), frm)
    #     else:
    #         self.transmitToClient(RequestAck(*request.key), frm)
    #         data = request.operation.get(DATA)
    #         addNymTxn = self.graphStore.getAddNymTxn(origin)
    #         txnIds = [addNymTxn[TXN_ID], ] + self.graphStore. \
    #             getAddAttributeTxnIds(origin)
    #         # If sending transactions to a user then should send user's
    #         # sponsor creation transaction also
    #         if addNymTxn.get(ROLE) is None:
    #             sponsorNymTxn = self.graphStore.getAddNymTxn(
    #                 addNymTxn.get(f.IDENTIFIER.nm))
    #             txnIds = [sponsorNymTxn[TXN_ID], ] + txnIds
    #         # TODO: Remove this log statement
    #         logger.debug("{} getting replies for {}".format(self, txnIds))
    #         result = self.secondaryStorage.getReplies(*txnIds, seqNo=data)
    #         txns = sorted(list(result.values()), key=itemgetter(F.seqNo.name))
    #         lastTxn = str(txns[-1][F.seqNo.name]) if len(txns) > 0 else data
    #         result = {
    #             TXN_ID: self.genTxnId(
    #                 request.identifier, request.reqId)
    #         }
    #         result.update(request.operation)
    #         # TODO: We should have a single JSON encoder which does the
    #         # encoding for us, like sorting by keys, handling datetime objects.
    #         result[DATA] = json.dumps({
    #             LAST_TXN: lastTxn,
    #             TXNS: txns
    #         }, default=dateTimeEncoding, sort_keys=True)
    #         result.update({
    #             f.IDENTIFIER.nm: request.identifier,
    #             f.REQ_ID.nm: request.reqId,
    #         })
    #         self.transmitToClient(Reply(result), frm)

    def processGetSchemaReq(self, request: Request, frm: str):
        issuerDid = request.operation[TARGET_NYM]
        schema = self.stateTreeStore.getSchema(
            did=issuerDid,
            schemaName=(request.operation[DATA][NAME]),
            schemaVersion=(request.operation[DATA][VERSION])
        )
        result = {
            TXN_ID: self.genTxnId(request.identifier, request.reqId)
        }
        result.update(request.operation)
        result[DATA] = schema
        result.update({
            f.IDENTIFIER.nm: request.identifier,
            f.REQ_ID.nm: request.reqId,
        })
        self.transmitToClient(Reply(result), frm)

    def processGetAttrsReq(self, request: Request, frm: str):
        self.transmitToClient(RequestAck(*request.key), frm)
        attrName = request.operation[RAW]
        nym = request.operation[TARGET_NYM]

        attrValue = self.stateTreeStore.getAttr(
            did=nym,
            key=attrName
        )

        # TODO: Implement selecting of seqNo
        # When graphStore was used it was a part of getRawAttr result, but
        # StateTreeStore does not store seqNo
        seqNo = "not implemented!"

        result = {
            TXN_ID: self.genTxnId(
                request.identifier, request.reqId)
        }
        if attrValue:
            attr = {attrName: attrValue}
            result[DATA] = json.dumps(attr, sort_keys=True)
            result[f.SEQ_NO.nm] = seqNo
        result.update(request.operation)
        result.update({
            f.IDENTIFIER.nm: request.identifier,
            f.REQ_ID.nm: request.reqId,
        })
        self.transmitToClient(Reply(result), frm)

    def processGetIssuerKeyReq(self, request: Request, frm: str):
        self.transmitToClient(RequestAck(*request.key), frm)
        keys = self.stateTreeStore.getIssuerKey(
            did=request.operation[ORIGIN],
            schemaSeqNo=request.operation[REF]
        )
        result = {
            TXN_ID: self.genTxnId(request.identifier, request.reqId)
        }
        result.update(request.operation)
        result[DATA] = keys
        result.update({
            f.IDENTIFIER.nm: request.identifier,
            f.REQ_ID.nm: request.reqId,
        })
        self.transmitToClient(Reply(result), frm)

    def processRequest(self, request: Request, frm: str):
        if request.operation[TXN_TYPE] == GET_NYM:
            self.processGetNymReq(request, frm)
        # TODO: Come back to it
        # elif request.operation[TXN_TYPE] == GET_TXNS:
        #     self.processGetTxnReq(request, frm)
        elif request.operation[TXN_TYPE] == GET_SCHEMA:
            self.processGetSchemaReq(request, frm)
        elif request.operation[TXN_TYPE] == GET_ATTR:
            self.processGetAttrsReq(request, frm)
        elif request.operation[TXN_TYPE] == GET_ISSUER_KEY:
            self.processGetIssuerKeyReq(request, frm)
        else:
            super().processRequest(request, frm)

    def storeTxnAndSendToClient(self, reply):
        """
        Does 4 things in following order
         1. Add reply to ledger.
         2. Send the reply to client.
         3. Add the reply to identity graph if needed.
         4. Add the reply to storage so it can be served later if the
         client requests it.
        """
        result = reply.result
        txnWithMerkleInfo = self.storeTxnInLedger(result)
        if result[TXN_TYPE] == NODE_UPGRADE:
            logger.info('{} processed {}'.format(self, NODE_UPGRADE))
            # Returning since NODE_UPGRADE is not sent to client and neither
            # goes in graph
            return
        self.sendReplyToClient(Reply(txnWithMerkleInfo),
                               (result[f.IDENTIFIER.nm], result[f.REQ_ID.nm]))
        reply.result[F.seqNo.name] = txnWithMerkleInfo.get(F.seqNo.name)
        self.storeTxn(reply.result)

    @staticmethod
    def ledgerId(txnType: str):
        if txnType in POOL_TXN_TYPES:
            return POOL_LEDGER_ID
        if txnType in IDENTITY_TXN_TYPES:
            return DOMAIN_LEDGER_ID
        if txnType in CONFIG_TXN_TYPES:
            return CONFIG_LEDGER_ID

    def storeTxnInLedger(self, result):
        if result[TXN_TYPE] == ATTRIB:
            result = self.hashAttribTxn(result)
        merkleInfo = self.appendResultToLedger(result)
        result.update(merkleInfo)
        return result

    @staticmethod
    def hashAttribTxn(result):
        # Creating copy of result so that `RAW`, `ENC` or `HASH` can be
        # replaced by their hashes. We do not insert actual attribute data
        # in the ledger but only the hash of it.
        result = deepcopy(result)
        if RAW in result:
            result[RAW] = sha256(result[RAW].encode()).hexdigest()
            # TODO: add checking for a number of keys in json
        elif ENC in result:
            result[ENC] = sha256(result[ENC].encode()).hexdigest()
        elif HASH in result:
            result[HASH] = result[HASH]
        else:
            error("Transaction missing required field")
        return result

    def storeTxn(self, txn) -> None:
        """
        Adds transaction to external non-ledger store
        """
        if txn[TXN_TYPE] == NYM:
            # TODO: this one should be stored in verkey store
            self.storeTxnInGraph(txn)
        elif txn[TXN_TYPE] in {ATTRIB, SCHEMA, ISSUER_KEY}:
            self.storeTxnInStateTree(txn)
        else:
            logger.debug("Got an unknown type {} to process".
                         format(txn[TXN_TYPE]))

    def storeTxnInStateTree(self, txn) -> None:
        did = txn[TARGET_NYM]
        self.stateTreeStore.addTxn(txn)

    def storeTxnInGraph(self, txn) -> None:
        txn = deepcopy(txn)
        # Remove root hash and audit path from result if present since they can
        # be generated on the fly from the ledger so no need to store it
        txn.pop(F.rootHash.name, None)
        txn.pop(F.auditPath.name, None)

        if txn[TXN_TYPE] == NYM:
            self.graphStore.addNymTxnToGraph(txn)
        elif txn[TXN_TYPE] == ATTRIB:
            self.graphStore.addAttribTxnToGraph(txn)
        elif txn[TXN_TYPE] == SCHEMA:
            self.graphStore.addSchemaTxnToGraph(txn)
        elif txn[TXN_TYPE] == ISSUER_KEY:
            self.graphStore.addIssuerKeyTxnToGraph(txn)
        else:
            logger.debug("Got an unknown type {} to process".
                         format(txn[TXN_TYPE]))

    def getReplyFor(self, request):
        typ = request.operation.get(TXN_TYPE)
        if typ in IDENTITY_TXN_TYPES:
            # result = self.secondaryStorage.getReply(request.identifier,
            #                                         request.reqId,
            #                                         type=request.operation[TXN_TYPE])
            reply = self.getReplyFromLedger(self.domainLedger, request)
            if reply:
                if request.operation[TXN_TYPE] == ATTRIB:
                    result = self.hashAttribTxn(reply.result)
                    reply = Reply(result)
                return reply
            else:
                return None
        if typ in CONFIG_TXN_TYPES:
            return self.getReplyFromLedger(self.configLedger, request)

    # def doCustomAction(self, ppTime: float, req: Request) -> None:
    def doCustomAction(self, ppTime, reqs: List[Request],
                       stateRoot, txnRoot) -> None:
        """
        Execute the REQUEST sent to this Node

        :param ppTime: the time at which PRE-PREPARE was sent
        :param req: the client REQUEST
        """
        if req.operation[TXN_TYPE] == NYM and not \
                self.canNymRequestBeProcessed(req.identifier, req.operation):
            reason = "nym {} is already added".format(req.operation[TARGET_NYM])
            if req.key in self.requestSender:
                self.transmitToClient(RequestNack(*req.key, reason),
                                      self.requestSender.pop(req.key))
        else:
            reply = self.generateReply(int(ppTime), req)
            self.storeTxnAndSendToClient(reply)
            if req.operation[TXN_TYPE] in CONFIG_TXN_TYPES:
                # Currently config ledger has only code update related changes
                # so transaction goes to Upgrader
                self.upgrader.handleUpgradeTxn(reply.result)

    # def generateReply(self, ppTime: float, req: Request):
    #     operation = req.operation
    #     txnId = self.genTxnId(req.identifier, req.reqId)
    #     result = {TXN_ID: txnId, TXN_TIME: int(ppTime)}
    #     result.update(operation)
    #     result.update({
    #         f.IDENTIFIER.nm: req.identifier,
    #         f.REQ_ID.nm: req.reqId,
    #     })
    #
    #     return Reply(result)
