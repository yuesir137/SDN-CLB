import logging

from ryu.base import app_manager
from ryu.ofproto import ofproto_v1_3
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, HANDSHAKE_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.topology.api import get_switch, get_link
from ryu.topology import event
from ryu.lib.packet import packet, tcp
from ryu.lib.packet import ethernet, ether_types, arp, ipv4
from ryu.lib import hub

import networkx as nx
import time
import socket
import queue
import my_load_balancer
import json
import math
import pickle
import os
import TOPSIS
import pandas as pd
import lock


#
class PathForward(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(PathForward, self).__init__(*args, **kwargs)
        self.G = nx.DiGraph()
        self.paths = {}
        self.hasPaths = False
        self.sendPath = False
        # 作为get_switch()和get_link()方法的参数传入
        self.topology_api_app = self
        #
        self.controllerId = 1
        self.mac_to_port = {}
        self.datapaths = {}
        self.initRole = False
        self.initedSWNum = 0
        self.initedConNum = 0
        self.initCon = 0
        # self.startTime = time.time()
        # print('开始于：{}'.format(self.startTime))

        self.send_queue = hub.Queue(16)
        self.socket = socket.socket()
        self.start_serve('127.0.0.1', 8888)
        print('start_serve')

        self.monitor_thread = hub.spawn(self._monitor)
        self.get_Ram = hub.spawn(self._getRam)
        self.lastMonitorTime = time.time()
        self.packetInRate = 0
        self.packetInCount = 0
        self.lastPacketInCount = 0

        self.respondTime = 0

        self.localDatapaths = {}
        self.localDatapathsRate = {}
        self.otherRAM = {}

        self.dpFlowTableNum = {} #交换机的流表数
        self.eachFlowData={}
        # {dpid:{ethe_address: port,...},...}
        # 控制器通信相关

        # barrier相关
        self.barrier_reply_Count = 0
        # 迁移过程变量
        self.startMigration = False  # 开始迁移的标志
        self.deleteFlag = False  # 收到删除流表的标志
        self.otherDel=False
        self.srcSlave = False

        self.endMigration = False  # 结束迁移
        self.requestControllerID = 0  # 请求迁移的控制器
        self.requestDatapaths = []  # 请求迁移的交换机列表
        self.fromController = False
        self.toController = False
        self.cachePacketIn = queue.Queue()

    def _monitor(self):
        """
        初始化，并监控交换机，超载则发送交换机迁移信息
        """
        while True:
            # print("start monitor at {}".format(time.time()))
            if not self.initRole and self.sendPath and self.initCon == 0:
                dp1 = self.datapaths[1]
                ofp = dp1.ofproto
                print('分配交换机')
                for i in range(1, 17):
                    dpConNum = math.ceil(i / 4)
                    dp = self.datapaths[i]
                    if self.controllerId == dpConNum:
                        self.send_role_request(dp, ofp.OFPCR_ROLE_MASTER)
                        self.localDatapaths[i] = dp
                        self.localDatapathsRate.setdefault(i, {})
                        self.localDatapathsRate[i]['lastPacketInCount'] = 0
                        self.localDatapathsRate[i]['packetInCount'] = 0
                        self.localDatapathsRate[i]['packetInRate'] = 0
                        self.localDatapathsRate[i]['packetInRateWind'] = []
                        numDict = {'this num': 0,
                                   'sum num': 0,
                                   'times': 0,
                                   'avg num': 0}
                        self.dpFlowTableNum[i] = numDict
                    else:
                        self.send_role_request(dp, ofp.OFPCR_ROLE_SLAVE)

                self.initCon = self.initCon + 1
                myport = str(self.controllerId + 6652)
                print('cgroup to limit controller cpu')
                result = os.popen('ps -ef|grep ryu-manager').readlines()
                for r in result:
                    # print('result:', r)
                    if myport in r:
                        process = r.split()[1]
                        print(process)
                        os.system('echo {} |sudo tee /sys/fs/cgroup/cpu/controller{}/tasks'.format(process,
                                                                                                   self.controllerId))

                self.get_Table_Num = hub.spawn(self._getTableNum)

            if self.initedConNum != 4:
                f = open('initedController', 'r')
                lines = f.readlines()
                self.initedConNum = len(lines)
                f.close()

            if self.initRole and self.initedConNum == 4:
                # print('进入监控 at {}'.format(time.time()))
                for dpId in self.localDatapathsRate.keys():
                    delta = self.localDatapathsRate[dpId]['packetInCount'] - self.localDatapathsRate[dpId][
                        'lastPacketInCount']
                    self.localDatapathsRate[dpId]['lastPacketInCount'] = self.localDatapathsRate[dpId]['packetInCount']
                    thisRate = delta / (time.time() - self.lastMonitorTime)

                    if len(self.localDatapathsRate[dpId]['packetInRateWind']) >= 3:
                        self.localDatapathsRate[dpId]['packetInRateWind'].pop(0)
                        self.localDatapathsRate[dpId]['packetInRateWind'].append(thisRate)
                    else:
                        self.localDatapathsRate[dpId]['packetInRateWind'].append(thisRate)
                    self.localDatapathsRate[dpId]['packetInRate'] = sum(
                        self.localDatapathsRate[dpId]['packetInRateWind']) / len(
                        self.localDatapathsRate[dpId]['packetInRateWind'])
                    # self.localDatapathsRate[dpId]['packetInRate']=thisRate

                self.lastMonitorTime = time.time()
                fileData = {self.controllerId: self.localDatapathsRate}

                # print('写入：{}'.format(fileData))
                if self.myFile:
                    self.myFile.seek(0)
                    self.myFile.truncate()
                    self.myFile.write(json.dumps(fileData))
                    self.myFile.flush()
                    # print('完成写入：at {}'.format(time.time()))

                myLoad = 0
                otherLoads = {}
                for dpid in self.localDatapathsRate.keys():
                    myLoad = myLoad + self.localDatapathsRate[dpid]['packetInRate']

                for other in self.files:
                    other.seek(0)
                    try:
                        data = json.load(other)
                        otherController = 0
                        otherControllerLoad = 0
                        for controller in data.keys():
                            load = 0
                            # print('controller' + controller + '--------', end=' ')
                            otherController = controller
                            for switch in data[controller]:
                                load = load + data[controller][switch]['packetInRate']
                                # print('switch' + switch + ': ', data[controller][switch]['packetInRate'], end='   ')
                            otherControllerLoad = load
                        otherLoads[otherController] = otherControllerLoad
                    except json.JSONDecodeError as e:
                        print(e)
                        # print('')
                avgLoad = (myLoad + sum(otherLoads.values())) / 4
                LBR = 0
                if avgLoad != 0:
                    LBR = 1 - (abs(myLoad - avgLoad) + sum(abs(v - avgLoad) for v in otherLoads.values())) / (
                                4 * avgLoad)
                print('myLoad: ', myLoad)
                print('detail:', self.localDatapathsRate)
                print('otherLoads: ', otherLoads)
                print('respondTime: ', self.respondTime)
                print('LBR: ', LBR)
                print('Table Num: ', self.dpFlowTableNum)

                self.respondTimelogger.info(
                    "{},{},{},{},{},{}".format(time.time(), self.respondTime, myLoad, LBR, self.controllerId,
                                               otherLoads))
                self.respondTime = 0
                # if myLoad>avgLoad+100:
                #     print('controller {} is overLoad!'.format(self.controllerId))
                # print('离开监控：at {}'.format(time.time()))

                if myLoad > 130 and myLoad > avgLoad and not self.startMigration and len(
                        otherLoads.keys()) == 3 and lock.get_lock(True):

                    # 发现超载，向目标控制器发送迁移请求

                    datapathId = 0
                    rate = 10000
                    for switch in self.localDatapathsRate.keys():
                        if 15< self.localDatapathsRate[switch]['packetInRate'] < rate:
                            datapathId = switch
                            rate = self.localDatapathsRate[switch]['packetInRate']
                    if datapathId==0:
                        datapathId=self.localDatapathsRate.keys()[0]
                    self.requestDatapaths.append(self.datapaths[datapathId])
                    self.startMigration = True
                    self.fromController = True
                    self.requestControllerID = self.controllerId
                    # self.requestDatapaths.append(self.datapaths[2])
                    print('find overload at ', time.time())
                    print('迁移交换机：S{}'.format(datapathId))
                    # otherControllerId = []
                    # for i in range(1, 5):
                    #     if i != self.controllerId:
                    #         otherControllerId.append(i)
                    start = time.time()
                    otherHop = TOPSIS.getOtherHop(str(datapathId), str(self.controllerId))
                    dataD = {'CPU': [v + 0.1 for v in otherLoads.values()], 'RAM': [v for v in self.otherRAM.values()],
                             'hop': [v for v in otherHop.values()]}
                    index = [k for k in otherHop.keys()]
                    data = pd.DataFrame(dataD, index=index)
                    data['CPU'] = 1 / data['CPU']
                    data['RAM'] = 1 / data['RAM']
                    data['hop'] = 1 / data['hop']
                    print('time spend at get data:', time.time() - start)
                    self.dstController = int(TOPSIS.esmlb(data, TOPSIS.WEIGHT))
                    # self.dstController = random.choice(otherControllerId)

                    self.respondTimelogger.info(
                        '{},migration switch{} from {} to {}, get controller spend time {}'.format(time.time(),
                                                                                                   datapathId,
                                                                                                   self.controllerId,
                                                                                                   self.dstController,
                                                                                                   time.time() - start))
                    message = json.dumps({'srcController': self.controllerId,
                                          'dstController': self.dstController, 'startMigration': 'True',
                                          'datapath': datapathId
                                          })
                    self.send(message)

            hub.sleep(0.5)

    def _getTableNum(self):
        while True:
            try:
                if self.startMigration!=True:
                    # print('get flow table number!!!')
                    for dp_id in self.dpFlowTableNum.keys():
                        print('get switch {} table num '.format(dp_id))
                        num = int(os.popen('sudo ovs-ofctl dump-flows s{} |wc -l'.format(dp_id)).readline().split()[0])
                        print('result is ',num)
                        sumNum = self.dpFlowTableNum[dp_id]['sum num'] + num
                        times = self.dpFlowTableNum[dp_id]['times'] + 1
                        avgNum = sumNum / times
                        numDict = {'this num': num,
                                   'sum num': sumNum,
                                   'times': times,
                                   'avg num': avgNum}
                        self.dpFlowTableNum[dp_id] = numDict
                time.sleep(0.5)
            except Exception as e:
                print(e)

    def getTableNum(self,dp_id):
        start=time.time()
        print('get switch {} table num at {}'.format(dp_id,start))
        num = int(os.popen('sudo ovs-ofctl dump-flows s{} |wc -l'.format(dp_id)).readline().split()[0])
        print('spend time ',time.time()-start)
        print('result is ', num)
        sumNum = self.dpFlowTableNum[dp_id]['sum num'] + num
        times = self.dpFlowTableNum[dp_id]['times'] + 1
        avgNum = sumNum / times
        numDict = {'this num': num,
                   'sum num': sumNum,
                   'times': times,
                   'avg num': avgNum}
        self.dpFlowTableNum[dp_id] = numDict

    def _getRam(self):
        while True:
            self.otherRAM = TOPSIS.getRAM(self.controllerId)

    def openFile(self):
        self.files = []
        self.myFile = open('con{}Rate.json'.format(self.controllerId), 'w')
        self.myFile.write(json.dumps(self.localDatapathsRate))
        self.myFile.flush()
        for i in range(1, 5):
            if i != self.controllerId:
                self.files.append(open("con{}Rate.json".format(i), 'r'))

    def start_serve(self, server_addr, server_port):
        """
        开启Socket通信
        """
        try:
            self.socket.connect((server_addr, server_port))
            self.status = True
            hub.spawn(self._rece_loop)
            hub.spawn(self._send_loop)
        except Exception as e:
            raise e

    def send(self, msg):
        """
        将消息放到消息队列，以供发送
        """
        if self.send_queue != None:
            self.send_queue.put(msg)

    def _send_loop(self):
        """
        监控消息队列，如果有消息，则发送出去
        """
        # print('self._send_loop()')
        try:
            while self.status:
                message = self.send_queue.get()
                message += '\n'
                print('send message at: ', time.time())
                print(message)
                self.socket.sendall(message.encode('utf-8'))
        finally:
            self.send_queue = None

    def rece_slave(self):
        try:
            message = self.socket.recv(128)
            message = message.decode('utf-8')
            # print('receive msg1:',message)
            if len(message) == 0:  # 关闭连接
                self.logger.info('connection fail, close')
            while '\n' != message[-1]:
                message += self.socket.recv(128).decode('utf-8')
            print('receive msg:', message)
            messageDict = eval(message)

            if 'slave' in messageDict.keys():
                if messageDict['slave'] == 'True':
                    self.srcSlave = True

        except ValueError:
            print(('Value error for %s, len: %d', message, len(message)))

    def _rece_loop(self):
        """
        控制器接收消息，并分析消息类型，以作进一步处理
        """
        while self.status:
            try:
                message = self.socket.recv(128)
                message = message.decode('utf-8')
                # print('receive msg1:',message)
                if len(message) == 0:  # 关闭连接
                    self.logger.info('connection fail, close')
                    self.status = False
                    break
                while '\n' != message[-1]:
                    message += self.socket.recv(128).decode('utf-8')
                print('receive msg:', message)
                messageDict = eval(message)
                # print('receive msg at:',time.time())

                if 'sendPath' in messageDict.keys():
                    print('get paths ...............')
                    self.paths = eval(messageDict['paths'])
                    f = open('netGraph', 'rb')
                    self.G = pickle.load(f)
                    self.hasPaths = True
                    self.sendPath = True

                if 'startMigration' in messageDict.keys():
                    print('receive migration request at:', time.time())
                    print('receive msg {} from {}:'.format(message, messageDict['srcController']))
                    if messageDict['startMigration'] == 'True' and not self.startMigration:
                        # 收到迁移请求，请求角色到EQUAL
                        self.startMigration = True  # 目标控制器的开始迁移标志变为TRUE，防止同时出现多个控制器迁移到相同的控制器，而发生冲突
                        self.toController = True
                        # self.file.write('{},{}'.format(time.time(), '请求到EQUAL'))
                        # self.file.flush()
                        self.requestControllerID = messageDict['srcController']
                        print('request to equal')
                        # TO DO：
                        # 将交换机换成列表的形式
                        dp = self.datapaths[messageDict['datapath']]
                        self.requestDatapaths.append(dp)
                        self.respondTimelogger.info(
                            '{},dst controller receive mig request at switch {} from controller {}'.format(time.time(),
                                                                                                           messageDict[
                                                                                                               'datapath'],
                                                                                                           self.requestControllerID))
                        # ofp_parse=dp.ofproto_parser
                        ofp = dp.ofproto
                        self.send_role_request(dp, ofp.OFPCR_ROLE_EQUAL)

                if 'ready' in messageDict.keys():  #
                    if messageDict['ready'] == 'True':
                        # 安装空流表，发送barrier消息
                        dp = self.datapaths[messageDict['datapath']]
                        ofp_parse = dp.ofproto_parser
                        ofp = dp.ofproto
                        # self.send_role_request(dp, ofp.OFPCR_ROLE_EQUAL)
                        out_port = 1234
                        in_port = 1234
                        actions = [ofp_parse.OFPActionOutput(out_port)]
                        match = ofp_parse.OFPMatch(in_port=in_port)
                        flags = ofp.OFPFF_SEND_FLOW_REM
                        self.add_flow1(dp, 1, match, actions, idle_timeout=0, flags=flags)
                        self.barrier_reply_Count = 0
                        self.send_barrier_request(dp)
                        print('安装空流表，发送barrier消息 at {}'.format(time.time()))

                if 'delFlowTable' in messageDict.keys():
                    if messageDict['delFlowTable']=='True':
                        self.otherDel=True



                if 'cmd' in messageDict.keys():
                    if messageDict['cmd'] == 'set_id':
                        self.controllerId = messageDict['client_id']
                        print('get controller id {}'.format(self.controllerId))
                        if self.controllerId == 1:
                            self.hasPaths = True
                        self.openFile()
                        self.respondTimelogger = logging.getLogger('respondTime')
                        self.respondTimelogger.setLevel(level=logging.DEBUG)
                        self.respondTimehandler = logging.FileHandler('respondTime{}.csv'.format(self.controllerId),
                                                                      encoding='UTF-8')
                        self.respondTimelogger.addHandler(self.respondTimehandler)

                if 'endMigration' in messageDict.keys():
                    if messageDict['endMigration'] == 'True':
                        self.endMigration = True
                        # self.file.write('{},{}'.format(time.time(), '结束迁移'))
                        # self.file.flush()
                        print('收到结束迁移于：', time.time())
                        dpid = messageDict['datapath']
                        dp = self.datapaths[dpid]
                        ofp = dp.ofproto
                        self.send_role_request(dp, ofp.OFPCR_ROLE_MASTER)
                        print('请求到MASTER')
                        print('迁移后获取各交换机流表')
                        self.getTableNum(dpid)

                        self.fromController = False
                        self.toController = False
                        print('处理缓存的消息')
                        while not self.cachePacketIn.empty():
                            ev = self.cachePacketIn.get()
                            self.cache_packet_in_handler(ev)
                        while self.srcSlave==False:
                            self.rece_slave()
                        print('处理完成，结束迁移')
                        lock.write_lock()
                        self.respondTimelogger.info('{},end migration'.format(time.time()))
                        # self.requestDatapaths.remove(dp)
                        self.startMigration = False  # 开始迁移的标志
                        self.deleteFlag = False  # 收到删除流表的标志
                        self.endMigration = False  # 结束迁移
                        self.requestControllerID = 0  # 请求迁移的控制器
                        self.requestDatapaths = []
                        self.srcSlave=False

            except ValueError:
                print(('Value error for %s, len: %d', message, len(message)))

    # 添加流表项的方法
    def add_flow(self, datapath, priority, match, actions):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        command = ofp.OFPFC_ADD
        inst = [ofp_parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        req = ofp_parser.OFPFlowMod(datapath=datapath, command=command,
                                    priority=priority, match=match, instructions=inst)
        datapath.send_msg(req)

    def del_flow(self, datapath, match):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        cookie = cookie_mask = 0
        table_id = 0
        idle_timeout = 0
        hard_timeout = 0
        priority = 1
        buffer_id = ofp.OFP_NO_BUFFER
        match = parser.OFPMatch(in_port=1234)
        actions = []
        inst = []
        req = parser.OFPFlowMod(datapath, cookie, cookie_mask,
                                table_id, ofp.OFPFC_DELETE,
                                idle_timeout, hard_timeout,
                                priority, buffer_id,
                                1234, ofp.OFPG_ANY,
                                ofp.OFPFF_SEND_FLOW_REM,
                                match, inst)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        print('flow removed ............at time:{}, switch {}'.format(time.time(), dp.id))
        if msg.reason == ofp.OFPRR_IDLE_TIMEOUT:
            reason = 'IDLE TIMEOUT'
        elif msg.reason == ofp.OFPRR_HARD_TIMEOUT:
            reason = 'HARD TIMEOUT'
        elif msg.reason == ofp.OFPRR_DELETE:
            reason = 'DELETE'
            if self.startMigration == True:
                if self.toController == True:
                    self.deleteFlag = True
                    self.localDatapaths[dp.id] = dp
                    self.localDatapathsRate.setdefault(dp.id, {})
                    self.localDatapathsRate[dp.id]['lastPacketInCount'] = 0
                    self.localDatapathsRate[dp.id]['packetInCount'] = 0
                    self.localDatapathsRate[dp.id]['packetInRate'] = 0
                    self.localDatapathsRate[dp.id]['packetInRateWind'] = []
                    numDict = {'this num': 0,
                               'sum num': 0,
                               'times': 0,
                               'avg num': 0}
                    self.dpFlowTableNum[dp.id] = numDict
                    delMsg=json.dumps({'dstController': self.requestControllerID,
                                'srcController': self.controllerId,
                                'delFlowTable': 'True', "datapath": dp.id})
                    self.send(delMsg)

                if self.fromController == True:
                # self.file.write('{},{}\n'.format(time.time(), '已删除空流表'))
                # self.file.flush()
                    while  self.otherDel!=True:
                        time.sleep(0.05)
                    self.deleteFlag = True
                    self.localDatapathsRate.pop(dp.id)
                    self.dpFlowTableNum.pop(dp.id)
                    print('本控制器流表删除状态：{}, 其他控制器删除状态：{}'.format(self.deleteFlag,self.otherDel))
                    print('流表已删除，进入backout......at ', time.time())
                    self.send_barrier_request(dp)
                # time.sleep(5.310)
                # self.deleteFlag=False
                # print('结束迁移 at',time.time())
                # endMigrationMsg=json.dumps({'dstController':2,'endMigration':'True','datapath':dp.id})
                # self.send(endMigrationMsg)
                # print('请求为SLAVE')
                # self.send_role_request(dp,ofp.OFPCR_ROLE_SLAVE)
        elif msg.reason == ofp.OFPRR_GROUP_DELETE:
            reason = 'GROUP DELETE'
        else:
            reason = 'unknown'
        self.logger.info('OFPFlowRemoved received: '
                         'cookie=%d priority=%d reason=%s table_id=%d '
                         'duration_sec=%d duration_nsec=%d '
                         'idle_timeout=%d hard_timeout=%d '
                         'packet_count=%d byte_count=%d match.fields=%s',
                         msg.cookie, msg.priority, reason, msg.table_id,
                         msg.duration_sec, msg.duration_nsec,
                         msg.idle_timeout, msg.hard_timeout,
                         msg.packet_count, msg.byte_count, msg.match)

    def send_role_request(self, datapath, role):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        gen_id = my_load_balancer.get_gen_id(ofp, role)
        print('get gen_id:{}'.format(gen_id))
        req = ofp_parser.OFPRoleRequest(datapath, role, gen_id)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPErrorMsg,
                [HANDSHAKE_DISPATCHER, CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_msg_handler(self, ev):
        msg = ev.msg
        dp=msg.datapath
        if dp in self.requestDatapaths and \
            msg.type==ofproto_v1_3.OFPET_ROLE_REQUEST_FAILED and \
            msg.code==ofproto_v1_3.OFPRRFC_STALE:
            self.send_role_request(dp, ofproto_v1_3.OFPCR_ROLE_SLAVE)
        self.logger.info('OFPErrorMsg received: type=0x%02x code=0x%02x ',
                      msg.type, msg.code)

    @set_ev_cls(ofp_event.EventOFPRoleReply, MAIN_DISPATCHER)
    def role_reply_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        ofp = dp.ofproto
        if msg.role == ofp.OFPCR_ROLE_NOCHANGE:
            role = 'NOCHANGE'
        elif msg.role == ofp.OFPCR_ROLE_EQUAL:
            role = 'EQUAL'
            if dp in self.requestDatapaths:
                # dstController=self.requestControllerID
                readyMessage = json.dumps({'dstController': self.requestControllerID,
                                           'srcController': self.controllerId,
                                           'ready': 'True', "datapath": dpid})
                self.send(readyMessage)
                print('请求到EQUAL')
        elif msg.role == ofp.OFPCR_ROLE_MASTER:
            role = 'MASTER'
            if not self.initRole:
                self.initedSWNum = self.initedSWNum + 1
                if self.initedSWNum == 16:
                    print('controller {}  initialized at {}'.format(self.controllerId, time.time()))
                    f = open('initedController', 'a+')
                    f.write(str(self.controllerId) + '\n')
                    f.close()
                    self.initRole = True
        elif msg.role == ofp.OFPCR_ROLE_SLAVE:
            role = 'SLAVE'
            if not self.initRole:
                self.initedSWNum = self.initedSWNum + 1
                if self.initedSWNum == 16:
                    print('controller {}  initialized at {}'.format(self.controllerId, time.time()))
                    f = open('initedController', 'a+')
                    f.write(str(self.controllerId) + '\n')
                    f.close()
                    self.initRole = True

            if self.fromController == True and dp in self.requestDatapaths:
                slaveMessage = json.dumps({'dstController': self.dstController,
                                           'srcController': self.controllerId,
                                           'slave': 'True', "datapath": dpid})
                self.send(slaveMessage)
                self.startMigration = False  # 开始迁移的标志
                self.deleteFlag = False  # 收到删除流表的标志
                self.otherDel = False
                self.endMigration = False  # 结束迁移
                self.requestControllerID = 0  # 请求迁移的控制器
                self.requestDatapaths = []
                self.fromController = False
                self.toController = False
                self.dstController = 0
                self.respondTimelogger.info('{},src controller end migration'.format(time.time()))
        else:
            role = 'unknown'
        print('receive OFPRoleReply msg at', time.time())
        self.logger.info('OFPRoleReply received: '
                         'dpid=%s, role=%s generation_id=%d', dpid,
                         role, msg.generation_id)

    def send_barrier_request(self, datapath):
        """
        发送barrier_request消息
        """
        ofp_parser = datapath.ofproto_parser
        req = ofp_parser.OFPBarrierRequest(datapath)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPBarrierReply, MAIN_DISPATCHER)
    def barrier_reply_handler(self, ev):
        self.logger.info('OFPBarrierReply received')
        dp = ev.msg.datapath
        ofp_parse = dp.ofproto_parser
        ofp = dp.ofproto
        self.barrier_reply_Count = self.barrier_reply_Count + 1
        if self.barrier_reply_Count == 1 and self.startMigration == True and self.fromController == True:
            print('删除空流表————delete dummy flow at {} by controller {}'.format(time.time(), self.controllerId))
            match = ofp_parse.OFPMatch(in_port=1234)
            self.del_flow(dp, match)
        if self.barrier_reply_Count == 2 and self.startMigration == True and self.fromController == True:
            print('第二次barrier reply at {} at controller {}'.format(time.time(), self.controllerId))
            # time.sleep(5)
            # self.file.write('{},{}\n'.format(time.time(), '结束迁移'))
            # self.file.flush()
            # self.localDatapathsRate.pop(dp.id)
            endMigrationMsg = json.dumps({'dstController': self.dstController,
                                          'srcController': self.controllerId,
                                          'endMigration': 'True',
                                          'datapath': dp.id})
            self.send(endMigrationMsg)
            print('源控制器发送SLAVE请求')
            self.send_role_request(dp, ofp.OFPCR_ROLE_SLAVE)





    # 当控制器和交换机开始的握手动作完成后，进行table-miss(默认流表)的添加
    # 关于这一段代码的详细解析，参见：https://blog.csdn.net/weixin_40042248/article/details/115749340
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser

        # add table-miss
        match = ofp_parser.OFPMatch()
        actions = [ofp_parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(datapath=datapath, priority=0, match=match, actions=actions)

    @set_ev_cls(event.EventSwitchEnter)
    def get_topo(self, ev):

        switch_list = get_switch(self.topology_api_app)
        print('switch_list: ', switch_list)
        switches = []
        # 得到每个设备的id，并写入图中作为图的节点
        for switch in switch_list:
            if switch.dp.id not in self.datapaths.keys():
                self.datapaths[switch.dp.id] = switch.dp
                print(self.datapaths)
            switches.append(switch.dp.id)
        self.G.add_nodes_from(switches)
        if self.controllerId != 1:
            return
        link_list = get_link(self.topology_api_app)
        print('link_list length is {}: '.format(len(link_list)))
        links = []
        # 将得到的链路的信息作为边写入图中
        for link in link_list:
            print('{}'.format(link))
            # print('link src: {}, dst: {}'.format(link.src,link.dst))
            links.append((link.src.dpid, link.dst.dpid, {'attr_dict': {'port': link.src.port_no}}))
        self.G.add_edges_from(links)

        for link in link_list:
            links.append((link.dst.dpid, link.src.dpid, {'attr_dict': {'port': link.dst.port_no}}))
        self.G.add_edges_from(links)

    def get_out_port(self, datapath, src, dst, in_port):
        dpid = datapath.id
        # print('get path  from {} to {}'.format( src, dst))
        # 开始时，各个主机可能在图中不存在，因为开始ryu只获取了交换机的dpid，并不知道各主机的信息，
        # 所以需要将主机存入图中
        if src not in self.G:
            print('add host {} at switch {}'.format(src, dpid))
            self.G.add_node(src)
            self.G.add_edge(dpid, src, attr_dict={'port': in_port})
            self.G.add_edge(src, dpid)

        if dst in self.G and self.controllerId == 1:
            if (src, dst) not in self.paths.keys():
                path = nx.shortest_path(self.G, src, dst)
                print('get path:   {}'.format(path))
                self.paths[(src, dst)] = path
            else:
                path = self.paths[(src, dst)]
            # print(self.paths)
            if len(self.paths.keys()) == 16 * 15 and not self.sendPath:
                f = open('netGraph', 'wb')
                pickle.dump(self.G, f)
                f.close()
                for i in range(1, 5):
                    if self.controllerId != i:
                        message = json.dumps({'srcController': self.controllerId,
                                              'dstController': i, 'sendPath': 'True',
                                              'paths': str(self.paths)
                                              })
                        self.send(message)
                        time.sleep(3)
                self.sendPath = True

            next_hop = path[path.index(dpid) + 1]
            out_port = self.G[dpid][next_hop]['attr_dict']['port']
            # print('get path {} from {} to {} at switch {}'.format(path, src, dst, dpid))
            # print('out_port is:   {}'.format(out_port))

        elif dst in self.G and self.controllerId != 1 and self.hasPaths == True:
            if (src, dst) in self.paths:
                path = self.paths[(src, dst)]
                # print('get path {} from {} to {} at switch {}'.format(path,src,dst,dpid))
                next_hop = path[path.index(dpid) + 1]
                out_port = self.G[dpid][next_hop]['attr_dict']['port']
                # print('out_port is:   {}'.format(out_port))

        else:
            out_port = datapath.ofproto.OFPP_FLOOD
        return out_port

        # 添加流表项的方法

    def add_flow1(self, datapath, priority, match, actions, idle_timeout=5, hard_timeout=0, flags=0):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        command = ofp.OFPFC_ADD
        inst = [ofp_parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        req = ofp_parser.OFPFlowMod(datapath=datapath, command=command,
                                    priority=priority, match=match, instructions=inst,
                                    idle_timeout=idle_timeout,
                                    hard_timeout=hard_timeout,
                                    flags=flags)
        print('send flow mod to switch {}'.format(datapath.id))
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        self.packetInTime = time.time()
        msg = ev.msg
        datapath = msg.datapath
        ofp = datapath.ofproto

        if self.deleteFlag and self.fromController==True and datapath in self.requestDatapaths:  # 如果是源控制器，在收到删除流表信息后，忽略消packet_in消息
            if self.barrier_reply_Count == 1 and self.startMigration == True :
                print('忽略消息 from switch {} at controller {}'.format(datapath.id, self.controllerId))
                return
            # if self.barrier_reply_Count == 2 and self.startMigration == True :
            #     print('请求为SLAVE from switch {} at controller {}'.format(datapath.id, self.controllerId))
            #     self.send_role_request(datapath, ofp.OFPCR_ROLE_SLAVE)
            #     return
            # 目标控制器
        if not self.deleteFlag and self.toController == True and datapath in self.requestDatapaths:  # 如果是目标控制器，在收到删除流表消息前，忽略消息Packet_IN消息
            # print(self.packetInCount)
            print('忽略消息 from switch {} at controller {}'.format(datapath.id, self.controllerId))
            return
        if self.deleteFlag and \
                self.toController == True and \
                datapath in self.requestDatapaths and \
                not self.endMigration:
            # print(self.packetInCount)
            print('暂时缓存 from switch {} at controller {}'.format(datapath.id, self.controllerId))
            self.cachePacketIn.put(ev)
            return

        ofp_parser = datapath.ofproto_parser

        dpid = datapath.id
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP or eth.ethertype == ether_types.ETH_TYPE_IPV6:
            # ignore lldp packet
            # print('ignore lldp')
            return
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        # print(pkt)

        if isinstance(arp_pkt, arp.arp):
            dst = arp_pkt.dst_ip
            src = arp_pkt.src_ip
        if isinstance(ip_pkt, ipv4.ipv4):
            dst = ip_pkt.dst
            src = ip_pkt.src
        if isinstance(tcp_pkt, tcp.tcp):
            tcp_srcp = tcp_pkt.src_port
            tcp_dstp = tcp_pkt.dst_port

        flow=(src,dst,tcp_srcp,tcp_dstp)
        if datapath.id in self.localDatapathsRate.keys():
            self.localDatapathsRate[datapath.id]['packetInCount'] = self.localDatapathsRate[datapath.id][
                                                                        'packetInCount'] + 1

        # print('{}, packet in form {} to {} at {}'.format(self.packetInTime,src,dst,dpid))
        out_port = self.get_out_port(datapath, src, dst, in_port)
        if out_port == ofp.OFPP_FLOOD:
            # print('add host .................')
            return

        # hopDelay = TOPSIS.HOP[str(dpid)][str(self.controllerId)] / 100
        # print('hopDelay : {}'.format(hopDelay))
        # time.sleep(hopDelay)

        actions = [ofp_parser.OFPActionOutput(out_port, ofp.OFPCML_NO_BUFFER)]
        # if self.startMigration==False:
        #     out_port = 1234
        #     in_port = 1234
        #     actions = [ofp_parser.OFPActionOutput(out_port,ofp.OFPCML_NO_BUFFER)]
        #     match = ofp_parser.OFPMatch(in_port=in_port)
        #     flags = ofp.OFPFF_SEND_FLOW_REM
        #     self.add_flow1(datapath, 1, match, actions, flags=flags)
        #     self.startMigration=True
        # 如果执行的动作不是flood，那么此时应该依据流表项进行转发操作，所以需要添加流表到交换机
        # if out_port != ofp.OFPP_FLOOD and isinstance(ip_pkt,ipv4.ipv4):
        #     # print('add flow at switch {}'.format(datapath.id))
        #     # print('match : in_port={}, ipv4_dst={}, ipv4_src={}'.format(in_port, dst, src))
        #     match = ofp_parser.OFPMatch(in_port=in_port,eth_type=ether_types.ETH_TYPE_IP, ipv4_src=src, ipv4_dst=dst)
        #     self.add_flow1(datapath=datapath, priority=1, match=match, idle_timeout=5,actions=actions,flags=0)

        if self.initRole and \
                20 > self.dpFlowTableNum[dpid]['this num'] > 0 and \
                out_port != ofp.OFPP_FLOOD and isinstance(tcp_pkt, tcp.tcp):
            self.dpFlowTableNum[dpid]['this num']+=1
            match = ofp_parser.OFPMatch(in_port=in_port, eth_type=ether_types.ETH_TYPE_IP, ip_proto=6, ipv4_src=src,
                                        ipv4_dst=dst, tcp_src=tcp_srcp, tcp_dst=tcp_dstp)
            self.add_flow1(datapath=datapath, priority=1, match=match, idle_timeout=5, actions=actions, flags=0)

            # print('add flow at switch {}'.format(datapath.id))
            # print('match : in_port={}, ipv4_dst={}, ipv4_src={}'.format(in_port, dst, src))


        # if out_port != ofp.OFPP_FLOOD and isinstance(arp_pkt,arp.arp):
        #     # print('add flow at switch {}'.format(datapath.id))
        #     # print('match : in_port={}, ipv4_dst={}, ipv4_src={}'.format(in_port, dst, src))
        #     match = ofp_parser.OFPMatch(in_port=in_port,eth_type=ether_types.ETH_TYPE_ARP, arp_spa=src, arp_tpa=dst)
        #     self.add_flow1(datapath=datapath, priority=1, match=match, idle_timeout=5,actions=actions,flags=0)

        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data
        # 控制器指导执行的命令
        out = ofp_parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
        respondTime = time.time() - self.packetInTime

        if respondTime > self.respondTime:
            self.respondTime = respondTime
            print('respond time is {}'.format(self.respondTime))

    def cache_packet_in_handler(self, ev):
        # self.packetInTime = time.time()
        msg = ev.msg
        datapath = msg.datapath

        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser

        dpid = datapath.id
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP or eth.ethertype == ether_types.ETH_TYPE_IPV6:
            # ignore lldp packet
            # print('ignore lldp')
            return
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        # print(pkt)
        # if datapath.id in self.localDatapathsRate.keys():
        #     self.localDatapathsRate[datapath.id]['packetInCount'] = self.localDatapathsRate[datapath.id][
        #                                                                 'packetInCount'] + 1

        if isinstance(arp_pkt, arp.arp):
            dst = arp_pkt.dst_ip
            src = arp_pkt.src_ip
        if isinstance(ip_pkt, ipv4.ipv4):
            dst = ip_pkt.dst
            src = ip_pkt.src
        # print('packet in form {} to {} at {}'.format(src,dst,dpid))
        out_port = self.get_out_port(datapath, src, dst, in_port)
        if out_port == ofp.OFPP_FLOOD:
            # print('add host .................')
            return

        # hopDelay = TOPSIS.HOP[str(dpid)][str(self.controllerId)] / 100
        # print('hopDelay : {}'.format(hopDelay))
        # time.sleep(hopDelay)

        actions = [ofp_parser.OFPActionOutput(out_port, ofp.OFPCML_NO_BUFFER)]
        # if self.startMigration==False:
        #     out_port = 1234
        #     in_port = 1234
        #     actions = [ofp_parser.OFPActionOutput(out_port,ofp.OFPCML_NO_BUFFER)]
        #     match = ofp_parser.OFPMatch(in_port=in_port)
        #     flags = ofp.OFPFF_SEND_FLOW_REM
        #     self.add_flow1(datapath, 1, match, actions, flags=flags)
        #     self.startMigration=True
        # 如果执行的动作不是flood，那么此时应该依据流表项进行转发操作，所以需要添加流表到交换机
        # if out_port != ofp.OFPP_FLOOD and isinstance(ip_pkt,ipv4.ipv4):
        #     # print('add flow at switch {}'.format(datapath.id))
        #     # print('match : in_port={}, ipv4_dst={}, ipv4_src={}'.format(in_port, dst, src))
        #     match = ofp_parser.OFPMatch(in_port=in_port,eth_type=ether_types.ETH_TYPE_IP, ipv4_src=src, ipv4_dst=dst)
        #     self.add_flow1(datapath=datapath, priority=1, match=match, actions=actions,flags=0)

        # if out_port != ofp.OFPP_FLOOD and isinstance(arp_pkt,arp.arp):
        #     # print('add flow at switch {}'.format(datapath.id))
        #     # print('match : in_port={}, ipv4_dst={}, ipv4_src={}'.format(in_port, dst, src))
        #     match = ofp_parser.OFPMatch(in_port=in_port,eth_type=ether_types.ETH_TYPE_ARP, arp_spa=src, arp_tpa=dst)
        #     self.add_flow1(datapath=datapath, priority=1, match=match, idle_timeout=0,actions=actions,flags=0)

        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data
        # 控制器指导执行的命令
        out = ofp_parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
        # self.respondTime = time.time() - self.packetInTime + hopDelay
        # if self.respondTime > 0.2:
        #     print('will overload because respond time is {}'.format(self.respondTime))
#         ryu-manager  shortest_path_forward_ygb_1.py --observe-links --ofp-tcp-listen-port=6653
