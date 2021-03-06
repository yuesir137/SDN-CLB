from ryu.base import app_manager
from ryu.ofproto import ofproto_v1_3
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.topology.api import get_switch, get_link
from ryu.topology import event
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet,ether_types,arp,ipv4
from ryu.lib import hub

import networkx as nx
import time
import socket
import queue
import my_load_balancer
import json
import math
import pickle
"""
four controller 16 switches routing normally!!!!
"""
class PathForward(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(PathForward, self).__init__(*args, **kwargs)
        self.G = nx.DiGraph()
        self.paths={}
        self.hasPaths=False
        self.sendPath=False
        # 作为get_switch()和get_link()方法的参数传入
        self.topology_api_app = self
        #
        self.controllerId = 1
        self.mac_to_port = {}
        self.datapaths = {}
        self.initRole = False
        self.startTime = time.time()
        print('开始于：{}'.format(self.startTime))

        self.send_queue = hub.Queue(16)
        self.socket = socket.socket()
        self.start_serve('127.0.0.1', 8888)
        print('start_serve')

        self.monitor_thread = hub.spawn(self._monitor)
        # self.lastPacketInTime=time.time_ns()
        self.packetInRate = 0
        self.packetInCount = 0
        self.lastPacketInCount = 0
        self.localDatapaths = {}
        self.localDatapathsRate = {}

        # {dpid:{ethe_adress: port,...},...}
        # 控制器通信相关

        # barrier相关
        self.barrier_reply_Count = 0
        # 迁移过程变量
        self.startMigration = False  # 开始迁移的标志
        self.deleteFlag = False  # 收到删除流表的标志
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
            if not self.initRole and self.sendPath:
                dp1 = self.datapaths[1]
                ofp = dp1.ofproto
                print('分配交换机')
                for i in range(1, 17):
                    dpConNum=math.ceil(i/4)
                    dp = self.datapaths[i]
                    if self.controllerId == dpConNum:
                        self.send_role_request(dp, ofp.OFPCR_ROLE_MASTER)
                        self.localDatapaths[i]=dp
                        self.localDatapathsRate.setdefault(i,{})
                        self.localDatapathsRate[i]['lastPacketInCount']=0
                        self.localDatapathsRate[i]['packetInCount']=0
                        self.localDatapathsRate[i]['packetInRate']=0
                    else:
                        self.send_role_request(dp, ofp.OFPCR_ROLE_SLAVE)
                #
                # for i in range(5, 9):
                #     dp = self.datapaths[i]
                #     if self.controllerId == 2:
                #         self.send_role_request(dp, ofp.OFPCR_ROLE_MASTER)
                #         self.localDatapaths[i]=dp
                #         self.localDatapathsRate.setdefault(i,{})
                #         self.localDatapathsRate[i]['lastPacketInCount'] = 0
                #         self.localDatapathsRate[i]['packetInCount'] = 0
                #         self.localDatapathsRate[i]['packetInRate'] = 0
                #     else:
                #         self.send_role_request(dp, ofp.OFPCR_ROLE_SLAVE)

                self.initRole = True
                # result = os.popen('ps -ef|grep ryu-manager').readlines()
                # processes = []
                # for r in result:
                #     if 'myApp' in r:
                #         processes.append(r.split()[1])
                # if len(processes) >= 2:
                #     for p in processes:
                #         os.system('cpulimit -p {} -l 10 &'.format(p))

            # if self.initRole:
            #     # print('进入监控 at {}'.format(time.time()))
            #     for dpId in self.localDatapathsRate.keys():
            #         delta = self.localDatapathsRate[dpId]['packetInCount'] - self.localDatapathsRate[dpId]['lastPacketInCount']
            #         self.localDatapathsRate[dpId]['lastPacketInCount'] = self.localDatapathsRate[dpId]['packetInCount']
            #         self.localDatapathsRate[dpId]['packetInRate'] = delta
            #     fileData={self.controllerId:self.localDatapathsRate}
            #     # print('写入：{}'.format(fileData))
            #     if self.myFile:
            #         self.myFile.seek(0)
            #         self.myFile.truncate()
            #         self.myFile.write(json.dumps(fileData))
            #         self.myFile.flush()
            #         # print('完成写入：at {}'.format(time.time()))
            #
            #
            #     myLoad=0
            #     otherLoads= {}
            #     for dp in self.localDatapathsRate.keys():
            #         myLoad=myLoad+self.localDatapathsRate[dp]['packetInRate']
            #
            #     for other in self.files:
            #         other.seek(0)
            #         try:
            #             data=json.load(other)
            #             otherController=0
            #             otherControllerLoad=0
            #             for controller in data.keys():
            #                 load=0
            #                 # print('controller' + controller + '--------', end=' ')
            #                 otherController=controller
            #                 for switch in data[controller]:
            #                     load=load+data[controller][switch]['packetInRate']
            #                     # print('switch' + switch + ': ', data[controller][switch]['packetInRate'], end='   ')
            #                 otherControllerLoad=load
            #             otherLoads[otherController]=otherControllerLoad
            #         except json.JSONDecodeError as e:
            #             print(e)
            #             # print('')
            #     avgLoad=(sum(otherLoads.values()))/1
            #     print('myLoad: ',myLoad)
            #     # print('detail:',self.localDatapathsRate)
            #     # print('otherLoads: ',otherLoads)
            #     # print('respondTime: ',self.respondTime)
            #     # if myLoad>avgLoad+100:
            #     #     print('controller {} is overLoad!'.format(self.controllerId))
            #     # print('离开监控：at {}'.format(time.time()))
            #
            #
            #     if myLoad>avgLoad+200000 and not self.startMigration:
            #         # 发现超载，向目标控制器发送迁移请求
            #         datapath=0
            #         rate=sys.maxsize
            #         for switch in self.localDatapathsRate.keys():
            #             if self.localDatapathsRate[switch]['packetInRate']<rate:
            #                 datapath=switch
            #                 rate=self.localDatapathsRate[switch]['packetInRate']
            #         self.requestDatapaths.append(self.datapaths[datapath])
            #         self.startMigration = True
            #         self.fromController = True
            #         self.requestControllerID = self.controllerId
            #         # self.requestDatapaths.append(self.datapaths[2])
            #         print('find overload at ', time.time())
            #         print('迁移交换机：S{}'.format(datapath))
            #         if self.controllerId==1:
            #             self.dstController=2
            #         else:
            #             self.dstController=1
            #         message = json.dumps({'srcController': self.controllerId,
            #                               'dstController': self.dstController, 'startMigration': 'True', 'datapath': datapath
            #                               })
            #         self.send(message)
            # for dpid,dp in self.datapaths.items():
            #     ofp=dp.ofproto
            #     print('role request dp {}'.format(dpid))
            #     self.send_role_request(dp,ofp.OFPCR_ROLE_NOCHANGE)
            hub.sleep(1)

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
        print('self._send_loop()')
        try:
            while self.status:
                message = self.send_queue.get()
                message += '\n'
                print('send message at: ', time.time())
                print(message)
                self.socket.sendall(message.encode('utf-8'))
        finally:
            self.send_queue = None

    def _rece_loop(self):
        """
        控制器接收消息，并分析消息类型，以作进一步处理
        """
        while self.status:
            try:
                message = self.socket.recv(128)
                message = message.decode('utf-8')
                print('receive msg1:',message)
                if len(message) == 0:  # 关闭连接
                    self.logger.info('connection fail, close')
                    self.status = False
                    break
                while '\n' != message[-1]:
                    message += self.socket.recv(128).decode('utf-8')
                print('receive msg:',message)
                messageDict = eval(message)
                # print('receive msg at:',time.time())


                if 'sendPath' in messageDict.keys():
                    print('get paths ...............')
                    self.paths=eval(messageDict['paths'])
                    f = open('netGraph', 'rb')
                    self.G= pickle.load(f)
                    self.hasPaths=True
                    self.sendPath=True

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
                        self.add_flow(dp, 1, match, actions, flags=flags)
                        self.barrier_reply_Count = 0
                        self.send_barrier_request(dp)
                        print('安装空流表，发送barrier消息 at {}'.format(time.time()))

                if 'cmd' in messageDict.keys():
                    if messageDict['cmd'] == 'set_id':
                        self.controllerId = messageDict['client_id']
                        print('get controller id {}'.format(self.controllerId))
                        if self.controllerId==1:
                            self.hasPaths=True
                        # self.openFile()

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
                        print('处理缓存的消息')
                        self.fromController = False
                        self.toController = False
                        while not self.cachePacketIn.empty():
                            ev = self.cachePacketIn.get()
                            self._packet_in_handler(ev)
                        print('处理完成，结束迁移')
                        # self.requestDatapaths.remove(dp)
                        self.startMigration = False  # 开始迁移的标志
                        self.deleteFlag = False  # 收到删除流表的标志
                        self.endMigration = False  # 结束迁移
                        self.requestControllerID = 0  # 请求迁移的控制器
                        self.requestDatapaths = []

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

    def send_role_request(self, datapath, role):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        gen_id = my_load_balancer.get_gen_id(ofp, role)
        print('get gen_id:{}'.format(gen_id))
        req = ofp_parser.OFPRoleRequest(datapath, role, gen_id)
        datapath.send_msg(req)

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
        elif msg.role == ofp.OFPCR_ROLE_SLAVE:
            role = 'SLAVE'
        else:
            role = 'unknown'
        print('receive OFPRoleReply msg at', time.time())
        self.logger.info('OFPRoleReply received: '
                         'dpid=%s, role=%s generation_id=%d', dpid,
                         role, msg.generation_id)


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
        print('switch_list: ',switch_list)
        switches = []
        # 得到每个设备的id，并写入图中作为图的节点
        for switch in switch_list:
            if switch.dp.id not in self.datapaths.keys():
                self.datapaths[switch.dp.id]=switch.dp
                print(self.datapaths)
            switches.append(switch.dp.id)
        self.G.add_nodes_from(switches)
        if self.controllerId!=1:
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
            print('add host {} at switch {}'.format(src,dpid))
            self.G.add_node(src)
            self.G.add_edge(dpid, src, attr_dict={'port': in_port})
            self.G.add_edge(src, dpid)


        if dst in self.G and self.controllerId==1:
            if (src,dst) not in self.paths.keys():
                path = nx.shortest_path(self.G, src, dst)
                print('get path:   {}'.format(path))
                self.paths[(src,dst)]=path
            else:
                path=self.paths[(src,dst)]
            # print(self.paths)
            if len(self.paths.keys())==16*15 and not self.sendPath:
                f = open('netGraph', 'wb')
                pickle.dump(self.G, f)
                f.close()
                for i in range(1,5):
                    if self.controllerId!=i:
                        message = json.dumps({'srcController': self.controllerId,
                                              'dstController': i, 'sendPath': 'True',
                                              'paths': str(self.paths)
                                              })
                        self.send(message)
                        time.sleep(3)
                self.sendPath=True
            next_hop = path[path.index(dpid) + 1]
            out_port = self.G[dpid][next_hop]['attr_dict']['port']
            print('get path {} from {} to {} at switch {}'.format(path, src, dst, dpid))
            print('out_port is:   {}'.format(out_port))

        elif dst in self.G and self.controllerId!=1 and self.hasPaths==True:
            if (src, dst) in self.paths:
                path = self.paths[(src, dst)]
                print('get path {} from {} to {} at switch {}'.format(path,src,dst,dpid))
                next_hop = path[path.index(dpid) + 1]
                out_port = self.G[dpid][next_hop]['attr_dict']['port']
                print('out_port is:   {}'.format(out_port))

        else:
            out_port = datapath.ofproto.OFPP_FLOOD
        return out_port

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
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
        if isinstance(arp_pkt,arp.arp):
            dst = arp_pkt.dst_ip
            src = arp_pkt.src_ip
        if isinstance(ip_pkt,ipv4.ipv4):
            dst = ip_pkt.dst
            src = ip_pkt.src
        # print('packet in form {} to {} at {}'.format(src,dst,dpid))
        out_port = self.get_out_port(datapath, src, dst, in_port)
        if out_port==ofp.OFPP_FLOOD:
            # print('add host .................')
            return
        actions = [ofp_parser.OFPActionOutput(out_port)]

        # 如果执行的动作不是flood，那么此时应该依据流表项进行转发操作，所以需要添加流表到交换机
        # if out_port != ofp.OFPP_FLOOD:
        #     match = ofp_parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
        #     self.add_flow(datapath=datapath, priority=1, match=match, actions=actions)

        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data
        # 控制器指导执行的命令
        out = ofp_parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
