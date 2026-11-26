-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- MES Detroit: MES conventions modeled after Rockwell Automation FactoryTalk
-- Third-party product names below describe the naming-convention style only (nominative use); not affiliated/endorsed. See repo NOTICE.
-- Run IDs (DET-RUN-NNNN), descriptive cell names (CNC-MILL-04), full operator names, Cp/Cpk SPC
SET search_path TO mes_detroit;

CREATE TABLE IF NOT EXISTS production_runs (
    run_id SERIAL PRIMARY KEY,
    run_number VARCHAR(20) NOT NULL,
    work_order_id VARCHAR(20),
    cell_id VARCHAR(20) NOT NULL,
    product_number VARCHAR(20),
    quantity_ordered INTEGER,
    quantity_produced INTEGER DEFAULT 0,
    quantity_defective INTEGER DEFAULT 0,
    run_start TIMESTAMP,
    run_end TIMESTAMP,
    run_status VARCHAR(20) CHECK (run_status IN ('IN_PROGRESS','COMPLETED','HALTED','PENDING')),
    operator_name VARCHAR(60),
    shift_number SMALLINT CHECK (shift_number IN (1,2,3))
);

CREATE TABLE IF NOT EXISTS quality_records (
    record_id SERIAL PRIMARY KEY,
    run_number VARCHAR(20) NOT NULL,
    inspection_type VARCHAR(20) CHECK (inspection_type IN ('INCOMING','IN_PROCESS','FINAL','SPC_STUDY')),
    characteristic VARCHAR(60) NOT NULL,
    target_value NUMERIC(10,4),
    lower_limit NUMERIC(10,4),
    upper_limit NUMERIC(10,4),
    actual_value NUMERIC(10,4),
    disposition VARCHAR(20) CHECK (disposition IN ('ACCEPT','REJECT','REWORK','DEVIATION')),
    inspection_time TIMESTAMP,
    inspector_name VARCHAR(60),
    cp_value NUMERIC(6,3),
    cpk_value NUMERIC(6,3)
);

-- production_runs: 120 rows, Feb–Mar 2026
-- Cells: CNC-MILL-04, CNC-LATHE-02, ROBOT-WELD-01
-- Products: 7742-A, 7742-B, 8801-C, 9910-D
INSERT INTO production_runs (run_number, work_order_id, cell_id, product_number, quantity_ordered, quantity_produced, quantity_defective, run_start, run_end, run_status, operator_name, shift_number)
SELECT v.run_number, v.work_order_id, v.cell_id, v.product_number, v.quantity_ordered::INTEGER, v.quantity_produced::INTEGER, v.quantity_defective::INTEGER, v.run_start::TIMESTAMP, v.run_end::TIMESTAMP, v.run_status, v.operator_name, v.shift_number::SMALLINT
FROM (VALUES
('DET-RUN-0001','DET-WO-1001','CNC-MILL-04','7742-A',200,198,2,'2026-02-02 06:00','2026-02-02 13:45','COMPLETED','John Stiles',1),
('DET-RUN-0002','DET-WO-1002','CNC-LATHE-02','7742-B',150,150,0,'2026-02-02 06:15','2026-02-02 12:30','COMPLETED','Mary Major',1),
('DET-RUN-0003','DET-WO-1003','ROBOT-WELD-01','8801-C',300,295,5,'2026-02-02 06:30','2026-02-02 14:00','COMPLETED','Mateo Jackson',1),
('DET-RUN-0004','DET-WO-1004','CNC-MILL-04','9910-D',100,100,0,'2026-02-02 14:00','2026-02-02 21:30','COMPLETED','Saanvi Sarkar',2),
('DET-RUN-0005','DET-WO-1005','CNC-LATHE-02','7742-A',175,172,3,'2026-02-02 14:15','2026-02-02 21:00','COMPLETED','Terry Whitlock',2),
('DET-RUN-0006','DET-WO-1006','ROBOT-WELD-01','7742-B',250,248,2,'2026-02-02 14:30','2026-02-02 22:00','COMPLETED','Li Juan',2),
('DET-RUN-0007','DET-WO-1007','CNC-MILL-04','8801-C',120,118,2,'2026-02-02 22:00','2026-02-03 05:30','COMPLETED','Carlos Salazar',3),
('DET-RUN-0008','DET-WO-1008','CNC-LATHE-02','9910-D',200,197,3,'2026-02-02 22:15','2026-02-03 05:45','COMPLETED','Efua Owusu',3),
('DET-RUN-0009','DET-WO-1009','ROBOT-WELD-01','7742-A',280,280,0,'2026-02-02 22:30','2026-02-03 06:00','COMPLETED','John Stiles',3),
('DET-RUN-0010','DET-WO-1010','CNC-MILL-04','7742-B',160,155,5,'2026-02-03 06:00','2026-02-03 13:30','COMPLETED','Mary Major',1),
('DET-RUN-0011','DET-WO-1011','CNC-LATHE-02','8801-C',220,220,0,'2026-02-03 06:15','2026-02-03 14:00','COMPLETED','Mateo Jackson',1),
('DET-RUN-0012','DET-WO-1012','ROBOT-WELD-01','9910-D',90,88,2,'2026-02-03 06:30','2026-02-03 12:45','COMPLETED','Saanvi Sarkar',1),
('DET-RUN-0013','DET-WO-1013','CNC-MILL-04','7742-A',200,200,0,'2026-02-03 14:00','2026-02-03 21:15','COMPLETED','Terry Whitlock',2),
('DET-RUN-0014','DET-WO-1014','CNC-LATHE-02','7742-B',175,170,5,'2026-02-03 14:15','2026-02-03 21:45','COMPLETED','Li Juan',2),
('DET-RUN-0015','DET-WO-1015','ROBOT-WELD-01','8801-C',300,298,2,'2026-02-03 14:30','2026-02-03 22:30','COMPLETED','Carlos Salazar',2),
('DET-RUN-0016','DET-WO-1016','CNC-MILL-04','9910-D',110,110,0,'2026-02-03 22:00','2026-02-04 05:00','COMPLETED','Efua Owusu',3),
('DET-RUN-0017','DET-WO-1017','CNC-LATHE-02','7742-A',190,185,5,'2026-02-03 22:15','2026-02-04 05:30','COMPLETED','John Stiles',3),
('DET-RUN-0018','DET-WO-1018','ROBOT-WELD-01','7742-B',260,260,0,'2026-02-03 22:30','2026-02-04 06:15','COMPLETED','Mary Major',3),
('DET-RUN-0019','DET-WO-1019','CNC-MILL-04','8801-C',140,136,4,'2026-02-04 06:00','2026-02-04 13:00','COMPLETED','Mateo Jackson',1),
('DET-RUN-0020','DET-WO-1020','CNC-LATHE-02','9910-D',200,200,0,'2026-02-04 06:15','2026-02-04 14:00','COMPLETED','Saanvi Sarkar',1),
('DET-RUN-0021','DET-WO-1021','ROBOT-WELD-01','7742-A',320,315,5,'2026-02-04 06:30','2026-02-04 15:00','COMPLETED','Terry Whitlock',1),
('DET-RUN-0022','DET-WO-1022','CNC-MILL-04','7742-B',180,178,2,'2026-02-04 14:00','2026-02-04 21:00','COMPLETED','Li Juan',2),
('DET-RUN-0023','DET-WO-1023','CNC-LATHE-02','8801-C',240,240,0,'2026-02-04 14:15','2026-02-04 22:00','COMPLETED','Carlos Salazar',2),
('DET-RUN-0024','DET-WO-1024','ROBOT-WELD-01','9910-D',95,90,5,'2026-02-04 14:30','2026-02-04 21:15','HALTED','Efua Owusu',2),
('DET-RUN-0025','DET-WO-1025','CNC-MILL-04','7742-A',200,200,0,'2026-02-04 22:00','2026-02-05 05:45','COMPLETED','John Stiles',3),
('DET-RUN-0026','DET-WO-1026','CNC-LATHE-02','7742-B',160,158,2,'2026-02-04 22:15','2026-02-05 05:30','COMPLETED','Mary Major',3),
('DET-RUN-0027','DET-WO-1027','ROBOT-WELD-01','8801-C',280,278,2,'2026-02-04 22:30','2026-02-05 06:00','COMPLETED','Mateo Jackson',3),
('DET-RUN-0028','DET-WO-1028','CNC-MILL-04','9910-D',120,120,0,'2026-02-05 06:00','2026-02-05 13:00','COMPLETED','Saanvi Sarkar',1),
('DET-RUN-0029','DET-WO-1029','CNC-LATHE-02','7742-A',200,196,4,'2026-02-05 06:15','2026-02-05 14:15','COMPLETED','Terry Whitlock',1),
('DET-RUN-0030','DET-WO-1030','ROBOT-WELD-01','7742-B',250,250,0,'2026-02-05 06:30','2026-02-05 14:30','COMPLETED','Li Juan',1),
('DET-RUN-0031','DET-WO-1031','CNC-MILL-04','8801-C',130,125,5,'2026-02-05 14:00','2026-02-05 21:30','COMPLETED','Carlos Salazar',2),
('DET-RUN-0032','DET-WO-1032','CNC-LATHE-02','9910-D',210,210,0,'2026-02-05 14:15','2026-02-05 22:00','COMPLETED','Efua Owusu',2),
('DET-RUN-0033','DET-WO-1033','ROBOT-WELD-01','7742-A',300,297,3,'2026-02-05 14:30','2026-02-05 22:45','COMPLETED','John Stiles',2),
('DET-RUN-0034','DET-WO-1034','CNC-MILL-04','7742-B',170,170,0,'2026-02-05 22:00','2026-02-06 05:30','COMPLETED','Mary Major',3),
('DET-RUN-0035','DET-WO-1035','CNC-LATHE-02','8801-C',230,226,4,'2026-02-05 22:15','2026-02-06 06:00','COMPLETED','Mateo Jackson',3),
('DET-RUN-0036','DET-WO-1036','ROBOT-WELD-01','9910-D',100,100,0,'2026-02-05 22:30','2026-02-06 05:15','COMPLETED','Saanvi Sarkar',3),
('DET-RUN-0037','DET-WO-1037','CNC-MILL-04','7742-A',200,198,2,'2026-02-09 06:00','2026-02-09 13:45','COMPLETED','Terry Whitlock',1),
('DET-RUN-0038','DET-WO-1038','CNC-LATHE-02','7742-B',155,155,0,'2026-02-09 06:15','2026-02-09 12:30','COMPLETED','Li Juan',1),
('DET-RUN-0039','DET-WO-1039','ROBOT-WELD-01','8801-C',290,285,5,'2026-02-09 06:30','2026-02-09 14:30','COMPLETED','Carlos Salazar',1),
('DET-RUN-0040','DET-WO-1040','CNC-MILL-04','9910-D',105,105,0,'2026-02-09 14:00','2026-02-09 21:00','COMPLETED','Efua Owusu',2),
('DET-RUN-0041','DET-WO-1041','CNC-LATHE-02','7742-A',185,182,3,'2026-02-09 14:15','2026-02-09 21:30','COMPLETED','John Stiles',2),
('DET-RUN-0042','DET-WO-1042','ROBOT-WELD-01','7742-B',260,258,2,'2026-02-09 14:30','2026-02-09 22:15','COMPLETED','Mary Major',2),
('DET-RUN-0043','DET-WO-1043','CNC-MILL-04','8801-C',125,120,5,'2026-02-09 22:00','2026-02-10 05:30','COMPLETED','Mateo Jackson',3),
('DET-RUN-0044','DET-WO-1044','CNC-LATHE-02','9910-D',205,205,0,'2026-02-09 22:15','2026-02-10 05:45','COMPLETED','Saanvi Sarkar',3),
('DET-RUN-0045','DET-WO-1045','ROBOT-WELD-01','7742-A',275,275,0,'2026-02-09 22:30','2026-02-10 06:00','COMPLETED','Terry Whitlock',3),
('DET-RUN-0046','DET-WO-1046','CNC-MILL-04','7742-B',165,162,3,'2026-02-10 06:00','2026-02-10 13:30','COMPLETED','Li Juan',1),
('DET-RUN-0047','DET-WO-1047','CNC-LATHE-02','8801-C',215,215,0,'2026-02-10 06:15','2026-02-10 14:00','COMPLETED','Carlos Salazar',1),
('DET-RUN-0048','DET-WO-1048','ROBOT-WELD-01','9910-D',92,88,4,'2026-02-10 06:30','2026-02-10 13:00','HALTED','Efua Owusu',1),
('DET-RUN-0049','DET-WO-1049','CNC-MILL-04','7742-A',200,200,0,'2026-02-10 14:00','2026-02-10 21:15','COMPLETED','John Stiles',2),
('DET-RUN-0050','DET-WO-1050','CNC-LATHE-02','7742-B',170,167,3,'2026-02-10 14:15','2026-02-10 21:45','COMPLETED','Mary Major',2),
('DET-RUN-0051','DET-WO-1051','ROBOT-WELD-01','8801-C',305,302,3,'2026-02-10 14:30','2026-02-10 23:00','COMPLETED','Mateo Jackson',2),
('DET-RUN-0052','DET-WO-1052','CNC-MILL-04','9910-D',115,115,0,'2026-02-10 22:00','2026-02-11 05:00','COMPLETED','Saanvi Sarkar',3),
('DET-RUN-0053','DET-WO-1053','CNC-LATHE-02','7742-A',195,190,5,'2026-02-10 22:15','2026-02-11 05:30','COMPLETED','Terry Whitlock',3),
('DET-RUN-0054','DET-WO-1054','ROBOT-WELD-01','7742-B',255,255,0,'2026-02-10 22:30','2026-02-11 06:15','COMPLETED','Li Juan',3),
('DET-RUN-0055','DET-WO-1055','CNC-MILL-04','8801-C',135,131,4,'2026-02-11 06:00','2026-02-11 13:00','COMPLETED','Carlos Salazar',1),
('DET-RUN-0056','DET-WO-1056','CNC-LATHE-02','9910-D',200,200,0,'2026-02-11 06:15','2026-02-11 14:00','COMPLETED','Efua Owusu',1),
('DET-RUN-0057','DET-WO-1057','ROBOT-WELD-01','7742-A',310,306,4,'2026-02-11 06:30','2026-02-11 15:00','COMPLETED','John Stiles',1),
('DET-RUN-0058','DET-WO-1058','CNC-MILL-04','7742-B',175,175,0,'2026-02-11 14:00','2026-02-11 21:00','COMPLETED','Mary Major',2),
('DET-RUN-0059','DET-WO-1059','CNC-LATHE-02','8801-C',235,232,3,'2026-02-11 14:15','2026-02-11 22:00','COMPLETED','Mateo Jackson',2),
('DET-RUN-0060','DET-WO-1060','ROBOT-WELD-01','9910-D',98,94,4,'2026-02-11 14:30','2026-02-11 21:45','HALTED','Saanvi Sarkar',2)
) AS v (run_number, work_order_id, cell_id, product_number, quantity_ordered, quantity_produced, quantity_defective, run_start, run_end, run_status, operator_name, shift_number)
WHERE NOT EXISTS (SELECT 1 FROM production_runs t WHERE t.run_number = v.run_number);

INSERT INTO production_runs (run_number, work_order_id, cell_id, product_number, quantity_ordered, quantity_produced, quantity_defective, run_start, run_end, run_status, operator_name, shift_number)
SELECT v.run_number, v.work_order_id, v.cell_id, v.product_number, v.quantity_ordered::INTEGER, v.quantity_produced::INTEGER, v.quantity_defective::INTEGER, v.run_start::TIMESTAMP, v.run_end::TIMESTAMP, v.run_status, v.operator_name, v.shift_number::SMALLINT
FROM (VALUES
('DET-RUN-0061','DET-WO-1061','CNC-MILL-04','7742-A',200,200,0,'2026-02-16 06:00','2026-02-16 13:30','COMPLETED','Terry Whitlock',1),
('DET-RUN-0062','DET-WO-1062','CNC-LATHE-02','7742-B',160,158,2,'2026-02-16 06:15','2026-02-16 12:45','COMPLETED','Li Juan',1),
('DET-RUN-0063','DET-WO-1063','ROBOT-WELD-01','8801-C',285,282,3,'2026-02-16 06:30','2026-02-16 14:15','COMPLETED','Carlos Salazar',1),
('DET-RUN-0064','DET-WO-1064','CNC-MILL-04','9910-D',108,108,0,'2026-02-16 14:00','2026-02-16 21:00','COMPLETED','Efua Owusu',2),
('DET-RUN-0065','DET-WO-1065','CNC-LATHE-02','7742-A',188,184,4,'2026-02-16 14:15','2026-02-16 21:45','COMPLETED','John Stiles',2),
('DET-RUN-0066','DET-WO-1066','ROBOT-WELD-01','7742-B',265,263,2,'2026-02-16 14:30','2026-02-16 22:30','COMPLETED','Mary Major',2),
('DET-RUN-0067','DET-WO-1067','CNC-MILL-04','8801-C',128,123,5,'2026-02-16 22:00','2026-02-17 05:30','COMPLETED','Mateo Jackson',3),
('DET-RUN-0068','DET-WO-1068','CNC-LATHE-02','9910-D',208,208,0,'2026-02-16 22:15','2026-02-17 05:45','COMPLETED','Saanvi Sarkar',3),
('DET-RUN-0069','DET-WO-1069','ROBOT-WELD-01','7742-A',278,278,0,'2026-02-16 22:30','2026-02-17 06:00','COMPLETED','Terry Whitlock',3),
('DET-RUN-0070','DET-WO-1070','CNC-MILL-04','7742-B',168,165,3,'2026-02-17 06:00','2026-02-17 13:30','COMPLETED','Li Juan',1),
('DET-RUN-0071','DET-WO-1071','CNC-LATHE-02','8801-C',218,218,0,'2026-02-17 06:15','2026-02-17 14:00','COMPLETED','Carlos Salazar',1),
('DET-RUN-0072','DET-WO-1072','ROBOT-WELD-01','9910-D',94,90,4,'2026-02-17 06:30','2026-02-17 13:15','HALTED','Efua Owusu',1),
('DET-RUN-0073','DET-WO-1073','CNC-MILL-04','7742-A',202,202,0,'2026-02-17 14:00','2026-02-17 21:15','COMPLETED','John Stiles',2),
('DET-RUN-0074','DET-WO-1074','CNC-LATHE-02','7742-B',172,169,3,'2026-02-17 14:15','2026-02-17 21:45','COMPLETED','Mary Major',2),
('DET-RUN-0075','DET-WO-1075','ROBOT-WELD-01','8801-C',295,293,2,'2026-02-17 14:30','2026-02-17 22:30','COMPLETED','Mateo Jackson',2),
('DET-RUN-0076','DET-WO-1076','CNC-MILL-04','9910-D',112,112,0,'2026-02-17 22:00','2026-02-18 05:00','COMPLETED','Saanvi Sarkar',3),
('DET-RUN-0077','DET-WO-1077','CNC-LATHE-02','7742-A',192,188,4,'2026-02-17 22:15','2026-02-18 05:30','COMPLETED','Terry Whitlock',3),
('DET-RUN-0078','DET-WO-1078','ROBOT-WELD-01','7742-B',258,258,0,'2026-02-17 22:30','2026-02-18 06:15','COMPLETED','Li Juan',3),
('DET-RUN-0079','DET-WO-1079','CNC-MILL-04','8801-C',138,134,4,'2026-02-18 06:00','2026-02-18 13:00','COMPLETED','Carlos Salazar',1),
('DET-RUN-0080','DET-WO-1080','CNC-LATHE-02','9910-D',202,202,0,'2026-02-18 06:15','2026-02-18 14:00','COMPLETED','Efua Owusu',1),
('DET-RUN-0081','DET-WO-1081','ROBOT-WELD-01','7742-A',312,308,4,'2026-02-18 06:30','2026-02-18 15:00','COMPLETED','John Stiles',1),
('DET-RUN-0082','DET-WO-1082','CNC-MILL-04','7742-B',178,178,0,'2026-02-18 14:00','2026-02-18 21:00','COMPLETED','Mary Major',2),
('DET-RUN-0083','DET-WO-1083','CNC-LATHE-02','8801-C',238,235,3,'2026-02-18 14:15','2026-02-18 22:00','COMPLETED','Mateo Jackson',2),
('DET-RUN-0084','DET-WO-1084','ROBOT-WELD-01','9910-D',96,92,4,'2026-02-18 14:30','2026-02-18 21:45','HALTED','Saanvi Sarkar',2),
('DET-RUN-0085','DET-WO-1085','CNC-MILL-04','7742-A',200,200,0,'2026-02-23 06:00','2026-02-23 13:30','COMPLETED','Terry Whitlock',1),
('DET-RUN-0086','DET-WO-1086','CNC-LATHE-02','7742-B',162,160,2,'2026-02-23 06:15','2026-02-23 12:45','COMPLETED','Li Juan',1),
('DET-RUN-0087','DET-WO-1087','ROBOT-WELD-01','8801-C',288,284,4,'2026-02-23 06:30','2026-02-23 14:30','COMPLETED','Carlos Salazar',1),
('DET-RUN-0088','DET-WO-1088','CNC-MILL-04','9910-D',110,110,0,'2026-02-23 14:00','2026-02-23 21:00','COMPLETED','Efua Owusu',2),
('DET-RUN-0089','DET-WO-1089','CNC-LATHE-02','7742-A',190,186,4,'2026-02-23 14:15','2026-02-23 21:45','COMPLETED','John Stiles',2),
('DET-RUN-0090','DET-WO-1090','ROBOT-WELD-01','7742-B',262,260,2,'2026-02-23 14:30','2026-02-23 22:15','COMPLETED','Mary Major',2),
('DET-RUN-0091','DET-WO-1091','CNC-MILL-04','8801-C',132,127,5,'2026-02-23 22:00','2026-02-24 05:30','COMPLETED','Mateo Jackson',3),
('DET-RUN-0092','DET-WO-1092','CNC-LATHE-02','9910-D',206,206,0,'2026-02-23 22:15','2026-02-24 05:45','COMPLETED','Saanvi Sarkar',3),
('DET-RUN-0093','DET-WO-1093','ROBOT-WELD-01','7742-A',282,282,0,'2026-02-23 22:30','2026-02-24 06:00','COMPLETED','Terry Whitlock',3),
('DET-RUN-0094','DET-WO-1094','CNC-MILL-04','7742-B',166,163,3,'2026-02-24 06:00','2026-02-24 13:30','COMPLETED','Li Juan',1),
('DET-RUN-0095','DET-WO-1095','CNC-LATHE-02','8801-C',222,222,0,'2026-02-24 06:15','2026-02-24 14:00','COMPLETED','Carlos Salazar',1),
('DET-RUN-0096','DET-WO-1096','ROBOT-WELD-01','9910-D',97,93,4,'2026-02-24 06:30','2026-02-24 13:30','HALTED','Efua Owusu',1),
('DET-RUN-0097','DET-WO-1097','CNC-MILL-04','7742-A',204,204,0,'2026-03-02 06:00','2026-03-02 13:30','COMPLETED','John Stiles',1),
('DET-RUN-0098','DET-WO-1098','CNC-LATHE-02','7742-B',158,156,2,'2026-03-02 06:15','2026-03-02 12:45','COMPLETED','Mary Major',1),
('DET-RUN-0099','DET-WO-1099','ROBOT-WELD-01','8801-C',292,289,3,'2026-03-02 06:30','2026-03-02 14:15','COMPLETED','Mateo Jackson',1),
('DET-RUN-0100','DET-WO-1100','CNC-MILL-04','9910-D',106,106,0,'2026-03-02 14:00','2026-03-02 21:00','COMPLETED','Saanvi Sarkar',2),
('DET-RUN-0101','DET-WO-1101','CNC-LATHE-02','7742-A',186,183,3,'2026-03-02 14:15','2026-03-02 21:30','COMPLETED','Terry Whitlock',2),
('DET-RUN-0102','DET-WO-1102','ROBOT-WELD-01','7742-B',256,254,2,'2026-03-02 14:30','2026-03-02 22:00','COMPLETED','Li Juan',2),
('DET-RUN-0103','DET-WO-1103','CNC-MILL-04','8801-C',126,121,5,'2026-03-02 22:00','2026-03-03 05:30','COMPLETED','Carlos Salazar',3),
('DET-RUN-0104','DET-WO-1104','CNC-LATHE-02','9910-D',204,204,0,'2026-03-02 22:15','2026-03-03 05:45','COMPLETED','Efua Owusu',3),
('DET-RUN-0105','DET-WO-1105','ROBOT-WELD-01','7742-A',276,276,0,'2026-03-02 22:30','2026-03-03 06:00','COMPLETED','John Stiles',3),
('DET-RUN-0106','DET-WO-1106','CNC-MILL-04','7742-B',164,161,3,'2026-03-03 06:00','2026-03-03 13:30','COMPLETED','Mary Major',1),
('DET-RUN-0107','DET-WO-1107','CNC-LATHE-02','8801-C',220,220,0,'2026-03-03 06:15','2026-03-03 14:00','COMPLETED','Mateo Jackson',1),
('DET-RUN-0108','DET-WO-1108','ROBOT-WELD-01','9910-D',93,89,4,'2026-03-03 06:30','2026-03-03 13:15','HALTED','Saanvi Sarkar',1),
('DET-RUN-0109','DET-WO-1109','CNC-MILL-04','7742-A',200,200,0,'2026-03-03 14:00','2026-03-03 21:15','COMPLETED','Terry Whitlock',2),
('DET-RUN-0110','DET-WO-1110','CNC-LATHE-02','7742-B',174,171,3,'2026-03-03 14:15','2026-03-03 21:45','COMPLETED','Li Juan',2),
('DET-RUN-0111','DET-WO-1111','ROBOT-WELD-01','8801-C',298,296,2,'2026-03-03 14:30','2026-03-03 22:30','COMPLETED','Carlos Salazar',2),
('DET-RUN-0112','DET-WO-1112','CNC-MILL-04','9910-D',114,114,0,'2026-03-03 22:00','2026-03-04 05:00','COMPLETED','Efua Owusu',3),
('DET-RUN-0113','DET-WO-1113','CNC-LATHE-02','7742-A',194,190,4,'2026-03-03 22:15','2026-03-04 05:30','COMPLETED','John Stiles',3),
('DET-RUN-0114','DET-WO-1114','ROBOT-WELD-01','7742-B',252,252,0,'2026-03-03 22:30','2026-03-04 06:15','COMPLETED','Mary Major',3),
('DET-RUN-0115','DET-WO-1115','CNC-MILL-04','8801-C',136,132,4,'2026-03-09 06:00','2026-03-09 13:00','COMPLETED','Mateo Jackson',1),
('DET-RUN-0116','DET-WO-1116','CNC-LATHE-02','9910-D',200,200,0,'2026-03-09 06:15','2026-03-09 14:00','COMPLETED','Saanvi Sarkar',1),
('DET-RUN-0117','DET-WO-1117','ROBOT-WELD-01','7742-A',308,304,4,'2026-03-09 06:30','2026-03-09 15:00','COMPLETED','Terry Whitlock',1),
('DET-RUN-0118','DET-WO-1118','CNC-MILL-04','7742-B',176,176,0,'2026-03-09 14:00','2026-03-09 21:00','COMPLETED','Li Juan',2),
('DET-RUN-0119','DET-WO-1119','CNC-LATHE-02','8801-C',232,229,3,'2026-03-09 14:15','2026-03-09 22:00','COMPLETED','Carlos Salazar',2),
('DET-RUN-0120','DET-WO-1120','ROBOT-WELD-01','9910-D',100,100,0,'2026-03-09 14:30','2026-03-09 22:00','IN_PROGRESS','Efua Owusu',2)
) AS v (run_number, work_order_id, cell_id, product_number, quantity_ordered, quantity_produced, quantity_defective, run_start, run_end, run_status, operator_name, shift_number)
WHERE NOT EXISTS (SELECT 1 FROM production_runs t WHERE t.run_number = v.run_number);

-- quality_records: 350 rows with SPC Cp/Cpk values
INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0001','INCOMING','Raw Stock Diameter',50.0000,49.9500,50.0500,50.0120,'ACCEPT','2026-02-02 05:30','Shirley Rodriguez',1.820,1.750),
('DET-RUN-0001','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0050,'ACCEPT','2026-02-02 08:00','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0001','IN_PROCESS','Surface Roughness',1.6000,0.8000,3.2000,1.4200,'ACCEPT','2026-02-02 09:30','Shirley Rodriguez',1.920,1.880),
('DET-RUN-0001','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0080,'ACCEPT','2026-02-02 13:30','Shirley Rodriguez',1.540,1.510),
('DET-RUN-0002','INCOMING','Raw Stock Length',120.0000,119.8000,120.2000,120.0300,'ACCEPT','2026-02-02 05:45','Pat Candella',1.780,1.740),
('DET-RUN-0002','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9950,'ACCEPT','2026-02-02 08:30','Pat Candella',1.700,1.680),
('DET-RUN-0002','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0180,'ACCEPT','2026-02-02 12:15','Pat Candella',1.860,1.820),
('DET-RUN-0003','INCOMING','Weld Wire Spool Check',0.9000,0.8800,0.9200,0.8950,'ACCEPT','2026-02-02 06:00','Shirley Rodriguez',2.100,2.050),
('DET-RUN-0003','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.1200,'ACCEPT','2026-02-02 09:00','Shirley Rodriguez',1.430,1.390),
('DET-RUN-0003','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,3.8800,'ACCEPT','2026-02-02 10:30','Shirley Rodriguez',1.550,1.490),
('DET-RUN-0003','FINAL','Tensile Strength',450.0000,420.0000,480.0000,448.0000,'ACCEPT','2026-02-02 13:45','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0004','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0080,'ACCEPT','2026-02-02 16:00','Pat Candella',1.580,1.550),
('DET-RUN-0004','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,5.9980,'ACCEPT','2026-02-02 17:00','Pat Candella',1.620,1.600),
('DET-RUN-0004','SPC_STUDY','Slot Position',0.0000,0.0000,0.1000,0.0320,'ACCEPT','2026-02-02 18:00','Pat Candella',1.750,1.710),
('DET-RUN-0004','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0200,'ACCEPT','2026-02-02 21:15','Pat Candella',1.490,1.460),
('DET-RUN-0005','INCOMING','Raw Stock Diameter',50.0000,49.9500,50.0500,50.0080,'ACCEPT','2026-02-02 13:45','Shirley Rodriguez',1.830,1.790),
('DET-RUN-0005','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0110,'ACCEPT','2026-02-02 16:00','Shirley Rodriguez',1.640,1.600),
('DET-RUN-0005','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0150,'ACCEPT','2026-02-02 20:45','Shirley Rodriguez',1.520,1.480),
('DET-RUN-0006','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.3500,'ACCEPT','2026-02-02 17:00','Pat Candella',1.410,1.350),
('DET-RUN-0006','FINAL','Tensile Strength',450.0000,420.0000,480.0000,445.0000,'ACCEPT','2026-02-02 21:45','Pat Candella',1.690,1.660),
('DET-RUN-0007','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,24.9920,'ACCEPT','2026-02-03 01:00','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0007','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.5800,'ACCEPT','2026-02-03 05:15','Shirley Rodriguez',1.900,1.870),
('DET-RUN-0008','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0220,'ACCEPT','2026-02-03 00:30','Pat Candella',1.560,1.520),
('DET-RUN-0008','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0050,'ACCEPT','2026-02-03 01:30','Pat Candella',1.600,1.580),
('DET-RUN-0008','FINAL','Overall Length',85.0000,84.9000,85.1000,84.9800,'ACCEPT','2026-02-03 05:30','Pat Candella',1.510,1.480),
('DET-RUN-0009','INCOMING','Weld Wire Spool Check',0.9000,0.8800,0.9200,0.9010,'ACCEPT','2026-02-02 22:00','Shirley Rodriguez',2.080,2.030),
('DET-RUN-0009','FINAL','Tensile Strength',450.0000,420.0000,480.0000,452.0000,'ACCEPT','2026-02-03 05:45','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0010','INCOMING','Raw Stock Diameter',50.0000,49.9500,50.0500,50.0250,'ACCEPT','2026-02-03 05:30','Pat Candella',1.800,1.740),
('DET-RUN-0010','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0180,'ACCEPT','2026-02-03 08:00','Pat Candella',1.620,1.570),
('DET-RUN-0010','IN_PROCESS','Surface Roughness',1.6000,0.8000,3.2000,2.1000,'ACCEPT','2026-02-03 09:30','Pat Candella',1.750,1.680),
('DET-RUN-0010','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0280,'ACCEPT','2026-02-03 13:15','Pat Candella',1.500,1.440),
('DET-RUN-0011','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0020,'ACCEPT','2026-02-03 09:00','Shirley Rodriguez',1.720,1.700),
('DET-RUN-0011','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0220,'ACCEPT','2026-02-03 13:45','Shirley Rodriguez',1.840,1.800),
('DET-RUN-0012','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,7.8800,'ACCEPT','2026-02-03 09:00','Pat Candella',1.450,1.410),
('DET-RUN-0012','FINAL','Tensile Strength',450.0000,420.0000,480.0000,441.0000,'ACCEPT','2026-02-03 12:30','Pat Candella',1.710,1.680),
('DET-RUN-0013','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,11.9880,'ACCEPT','2026-02-03 16:00','Shirley Rodriguez',1.590,1.560),
('DET-RUN-0013','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0120,'ACCEPT','2026-02-03 17:00','Shirley Rodriguez',1.610,1.590),
('DET-RUN-0013','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0400,'ACCEPT','2026-02-03 21:00','Shirley Rodriguez',1.480,1.450),
('DET-RUN-0014','INCOMING','Raw Stock Length',120.0000,119.8000,120.2000,119.9600,'ACCEPT','2026-02-03 13:45','Pat Candella',1.760,1.720),
('DET-RUN-0014','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9880,'ACCEPT','2026-02-03 16:30','Pat Candella',1.690,1.660),
('DET-RUN-0014','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0350,'ACCEPT','2026-02-03 21:30','Pat Candella',1.820,1.760),
('DET-RUN-0015','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.1200,'ACCEPT','2026-02-03 17:30','Shirley Rodriguez',1.530,1.490),
('DET-RUN-0015','FINAL','Tensile Strength',450.0000,420.0000,480.0000,455.0000,'ACCEPT','2026-02-03 22:15','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0016','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0050,'ACCEPT','2026-02-04 00:00','Pat Candella',1.570,1.540),
('DET-RUN-0016','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0100,'ACCEPT','2026-02-04 04:45','Pat Candella',1.500,1.470),
('DET-RUN-0017','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0090,'ACCEPT','2026-02-04 01:00','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0017','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0200,'ACCEPT','2026-02-04 05:15','Shirley Rodriguez',1.510,1.470),
('DET-RUN-0018','INCOMING','Weld Wire Spool Check',0.9000,0.8800,0.9200,0.8980,'ACCEPT','2026-02-03 22:00','Pat Candella',2.090,2.040),
('DET-RUN-0018','FINAL','Tensile Strength',450.0000,420.0000,480.0000,449.0000,'ACCEPT','2026-02-04 06:00','Pat Candella',1.670,1.640),
('DET-RUN-0019','INCOMING','Raw Stock Diameter',50.0000,49.9500,50.0500,50.0180,'ACCEPT','2026-02-04 05:30','Shirley Rodriguez',1.810,1.760),
('DET-RUN-0019','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0220,'ACCEPT','2026-02-04 08:00','Shirley Rodriguez',1.600,1.540),
('DET-RUN-0019','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.9500,'ACCEPT','2026-02-04 12:45','Shirley Rodriguez',1.870,1.820),
('DET-RUN-0020','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0100,'ACCEPT','2026-02-04 09:00','Pat Candella',1.710,1.690),
('DET-RUN-0020','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0280,'ACCEPT','2026-02-04 13:45','Pat Candella',1.850,1.810),
('DET-RUN-0021','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.2200,'ACCEPT','2026-02-04 09:30','Shirley Rodriguez',1.420,1.380),
('DET-RUN-0021','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,3.9500,'ACCEPT','2026-02-04 11:00','Shirley Rodriguez',1.560,1.510),
('DET-RUN-0021','FINAL','Tensile Strength',450.0000,420.0000,480.0000,447.0000,'ACCEPT','2026-02-04 14:45','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0022','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0150,'ACCEPT','2026-02-04 16:00','Pat Candella',1.580,1.550),
('DET-RUN-0022','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,5.9920,'ACCEPT','2026-02-04 17:00','Pat Candella',1.630,1.610),
('DET-RUN-0022','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0300,'ACCEPT','2026-02-04 20:45','Pat Candella',1.490,1.460),
('DET-RUN-0023','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9960,'ACCEPT','2026-02-04 16:30','Shirley Rodriguez',1.730,1.710),
('DET-RUN-0023','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0190,'ACCEPT','2026-02-04 21:45','Shirley Rodriguez',1.870,1.840),
('DET-RUN-0024','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.4800,'REWORK','2026-02-04 16:45','Pat Candella',0.980,0.820),
('DET-RUN-0024','FINAL','Tensile Strength',450.0000,420.0000,480.0000,418.0000,'REJECT','2026-02-04 21:00','Pat Candella',0.870,0.720),
('DET-RUN-0025','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0030,'ACCEPT','2026-02-05 01:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0025','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0060,'ACCEPT','2026-02-05 05:30','Shirley Rodriguez',1.530,1.500),
('DET-RUN-0026','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0080,'ACCEPT','2026-02-05 01:00','Pat Candella',1.700,1.680),
('DET-RUN-0026','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0240,'ACCEPT','2026-02-05 05:15','Pat Candella',1.830,1.790),
('DET-RUN-0027','FINAL','Tensile Strength',450.0000,420.0000,480.0000,451.0000,'ACCEPT','2026-02-05 05:45','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0028','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0020,'ACCEPT','2026-02-05 08:00','Pat Candella',1.600,1.570),
('DET-RUN-0028','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0080,'ACCEPT','2026-02-05 09:00','Pat Candella',1.620,1.600),
('DET-RUN-0028','FINAL','Overall Length',85.0000,84.9000,85.1000,84.9900,'ACCEPT','2026-02-05 12:45','Pat Candella',1.510,1.480),
('DET-RUN-0029','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0160,'ACCEPT','2026-02-05 09:00','Shirley Rodriguez',1.630,1.590),
('DET-RUN-0029','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0240,'ACCEPT','2026-02-05 14:00','Shirley Rodriguez',1.490,1.440),
('DET-RUN-0030','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.0500,'ACCEPT','2026-02-05 09:30','Pat Candella',1.460,1.430),
('DET-RUN-0030','FINAL','Tensile Strength',450.0000,420.0000,480.0000,453.0000,'ACCEPT','2026-02-05 14:15','Pat Candella',1.650,1.620),
('DET-RUN-0031','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0190,'ACCEPT','2026-02-05 16:30','Shirley Rodriguez',1.610,1.560),
('DET-RUN-0031','FINAL','Surface Roughness',1.6000,0.8000,3.2000,2.2500,'ACCEPT','2026-02-05 21:15','Shirley Rodriguez',1.840,1.780),
('DET-RUN-0032','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0310,'ACCEPT','2026-02-05 21:45','Pat Candella',1.820,1.770),
('DET-RUN-0033','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.0800,'ACCEPT','2026-02-05 17:00','Shirley Rodriguez',1.540,1.500),
('DET-RUN-0033','FINAL','Tensile Strength',450.0000,420.0000,480.0000,446.0000,'ACCEPT','2026-02-05 22:30','Shirley Rodriguez',1.670,1.640)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0034','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0030,'ACCEPT','2026-02-06 00:00','Pat Candella',1.590,1.560),
('DET-RUN-0034','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0200,'ACCEPT','2026-02-06 05:15','Pat Candella',1.500,1.470),
('DET-RUN-0035','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0040,'ACCEPT','2026-02-06 01:00','Shirley Rodriguez',1.720,1.700),
('DET-RUN-0035','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0260,'ACCEPT','2026-02-06 05:45','Shirley Rodriguez',1.840,1.800),
('DET-RUN-0036','FINAL','Tensile Strength',450.0000,420.0000,480.0000,450.0000,'ACCEPT','2026-02-06 05:00','Pat Candella',1.680,1.650),
('DET-RUN-0037','INCOMING','Raw Stock Diameter',50.0000,49.9500,50.0500,50.0090,'ACCEPT','2026-02-09 05:30','Shirley Rodriguez',1.820,1.780),
('DET-RUN-0037','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0070,'ACCEPT','2026-02-09 08:30','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0037','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0100,'ACCEPT','2026-02-09 13:30','Shirley Rodriguez',1.520,1.490),
('DET-RUN-0038','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9980,'ACCEPT','2026-02-09 09:00','Pat Candella',1.710,1.690),
('DET-RUN-0038','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0200,'ACCEPT','2026-02-09 12:15','Pat Candella',1.860,1.820),
('DET-RUN-0039','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.1800,'ACCEPT','2026-02-09 09:30','Shirley Rodriguez',1.430,1.390),
('DET-RUN-0039','FINAL','Tensile Strength',450.0000,420.0000,480.0000,444.0000,'ACCEPT','2026-02-09 14:15','Shirley Rodriguez',1.690,1.660),
('DET-RUN-0040','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0060,'ACCEPT','2026-02-09 16:00','Pat Candella',1.580,1.550),
('DET-RUN-0040','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0030,'ACCEPT','2026-02-09 17:00','Pat Candella',1.620,1.600),
('DET-RUN-0040','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0150,'ACCEPT','2026-02-09 20:45','Pat Candella',1.490,1.460),
('DET-RUN-0041','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0130,'ACCEPT','2026-02-09 16:30','Shirley Rodriguez',1.640,1.610),
('DET-RUN-0041','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.7200,'ACCEPT','2026-02-09 21:15','Shirley Rodriguez',1.890,1.850),
('DET-RUN-0042','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.0500,'ACCEPT','2026-02-09 17:00','Pat Candella',1.550,1.510),
('DET-RUN-0042','FINAL','Tensile Strength',450.0000,420.0000,480.0000,447.0000,'ACCEPT','2026-02-09 22:00','Pat Candella',1.670,1.640),
('DET-RUN-0043','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0200,'ACCEPT','2026-02-10 01:00','Shirley Rodriguez',1.600,1.540),
('DET-RUN-0043','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0290,'ACCEPT','2026-02-10 05:15','Shirley Rodriguez',1.480,1.420),
('DET-RUN-0044','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0290,'ACCEPT','2026-02-10 05:30','Pat Candella',1.830,1.780),
('DET-RUN-0045','FINAL','Tensile Strength',450.0000,420.0000,480.0000,453.0000,'ACCEPT','2026-02-10 05:45','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0046','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0100,'ACCEPT','2026-02-10 09:00','Pat Candella',1.630,1.600),
('DET-RUN-0046','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0170,'ACCEPT','2026-02-10 13:15','Pat Candella',1.500,1.460)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0047','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0060,'ACCEPT','2026-02-10 09:30','Shirley Rodriguez',1.710,1.690),
('DET-RUN-0047','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0210,'ACCEPT','2026-02-10 13:45','Shirley Rodriguez',1.850,1.810),
('DET-RUN-0048','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.4600,'REWORK','2026-02-10 09:00','Pat Candella',0.960,0.790),
('DET-RUN-0048','FINAL','Tensile Strength',450.0000,420.0000,480.0000,416.0000,'REJECT','2026-02-10 12:45','Pat Candella',0.850,0.700),
('DET-RUN-0049','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0010,'ACCEPT','2026-02-10 16:00','Shirley Rodriguez',1.600,1.570),
('DET-RUN-0049','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0060,'ACCEPT','2026-02-10 17:00','Shirley Rodriguez',1.620,1.600),
('DET-RUN-0049','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0050,'ACCEPT','2026-02-10 21:00','Shirley Rodriguez',1.510,1.480),
('DET-RUN-0050','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9920,'ACCEPT','2026-02-10 16:30','Pat Candella',1.700,1.680),
('DET-RUN-0050','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0330,'ACCEPT','2026-02-10 21:30','Pat Candella',1.820,1.760),
('DET-RUN-0051','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.1500,'ACCEPT','2026-02-10 17:30','Shirley Rodriguez',1.530,1.490),
('DET-RUN-0051','FINAL','Tensile Strength',450.0000,420.0000,480.0000,456.0000,'ACCEPT','2026-02-10 22:45','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0052','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0040,'ACCEPT','2026-02-11 00:00','Pat Candella',1.580,1.550),
('DET-RUN-0052','FINAL','Overall Length',85.0000,84.9000,85.1000,84.9950,'ACCEPT','2026-02-11 04:45','Pat Candella',1.500,1.470),
('DET-RUN-0053','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0170,'ACCEPT','2026-02-11 01:00','Shirley Rodriguez',1.620,1.580),
('DET-RUN-0053','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0230,'ACCEPT','2026-02-11 05:15','Shirley Rodriguez',1.490,1.440),
('DET-RUN-0054','FINAL','Tensile Strength',450.0000,420.0000,480.0000,450.0000,'ACCEPT','2026-02-11 06:00','Pat Candella',1.670,1.640),
('DET-RUN-0055','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0140,'ACCEPT','2026-02-11 09:00','Shirley Rodriguez',1.630,1.590),
('DET-RUN-0055','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.8800,'ACCEPT','2026-02-11 12:45','Shirley Rodriguez',1.870,1.820),
('DET-RUN-0056','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0030,'ACCEPT','2026-02-11 09:00','Pat Candella',1.720,1.700),
('DET-RUN-0056','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0250,'ACCEPT','2026-02-11 13:45','Pat Candella',1.840,1.800),
('DET-RUN-0057','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.1500,'ACCEPT','2026-02-11 09:30','Shirley Rodriguez',1.440,1.400),
('DET-RUN-0057','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,3.9200,'ACCEPT','2026-02-11 11:00','Shirley Rodriguez',1.560,1.510),
('DET-RUN-0057','FINAL','Tensile Strength',450.0000,420.0000,480.0000,448.0000,'ACCEPT','2026-02-11 14:45','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0058','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0080,'ACCEPT','2026-02-11 16:00','Pat Candella',1.590,1.560),
('DET-RUN-0058','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0250,'ACCEPT','2026-02-11 20:45','Pat Candella',1.490,1.460)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0059','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0070,'ACCEPT','2026-02-11 16:30','Shirley Rodriguez',1.710,1.690),
('DET-RUN-0059','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0270,'ACCEPT','2026-02-11 21:45','Shirley Rodriguez',1.830,1.790),
('DET-RUN-0060','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.4900,'REWORK','2026-02-11 16:45','Pat Candella',0.940,0.770),
('DET-RUN-0060','FINAL','Tensile Strength',450.0000,420.0000,480.0000,415.0000,'REJECT','2026-02-11 21:30','Pat Candella',0.840,0.690),
('DET-RUN-0061','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0060,'ACCEPT','2026-02-16 09:00','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0061','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0090,'ACCEPT','2026-02-16 13:15','Shirley Rodriguez',1.520,1.490),
('DET-RUN-0062','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9970,'ACCEPT','2026-02-16 09:00','Pat Candella',1.720,1.700),
('DET-RUN-0062','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0220,'ACCEPT','2026-02-16 12:30','Pat Candella',1.850,1.810),
('DET-RUN-0063','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.0800,'ACCEPT','2026-02-16 09:30','Shirley Rodriguez',1.450,1.410),
('DET-RUN-0063','FINAL','Tensile Strength',450.0000,420.0000,480.0000,449.0000,'ACCEPT','2026-02-16 14:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0064','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0020,'ACCEPT','2026-02-16 16:00','Pat Candella',1.600,1.570),
('DET-RUN-0064','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0040,'ACCEPT','2026-02-16 17:00','Pat Candella',1.620,1.600),
('DET-RUN-0064','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0100,'ACCEPT','2026-02-16 20:45','Pat Candella',1.500,1.470),
('DET-RUN-0065','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0120,'ACCEPT','2026-02-16 16:30','Shirley Rodriguez',1.640,1.610),
('DET-RUN-0065','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.6800,'ACCEPT','2026-02-16 21:30','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0066','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.0200,'ACCEPT','2026-02-16 17:00','Pat Candella',1.550,1.510),
('DET-RUN-0066','FINAL','Tensile Strength',450.0000,420.0000,480.0000,452.0000,'ACCEPT','2026-02-16 22:15','Pat Candella',1.660,1.630),
('DET-RUN-0067','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0180,'ACCEPT','2026-02-17 01:00','Shirley Rodriguez',1.610,1.560),
('DET-RUN-0067','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0260,'ACCEPT','2026-02-17 05:15','Shirley Rodriguez',1.480,1.420),
('DET-RUN-0068','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0300,'ACCEPT','2026-02-17 05:30','Pat Candella',1.820,1.770),
('DET-RUN-0069','FINAL','Tensile Strength',450.0000,420.0000,480.0000,454.0000,'ACCEPT','2026-02-17 05:45','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0070','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0090,'ACCEPT','2026-02-17 09:00','Pat Candella',1.640,1.610),
('DET-RUN-0070','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0140,'ACCEPT','2026-02-17 13:15','Pat Candella',1.510,1.470),
('DET-RUN-0071','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0050,'ACCEPT','2026-02-17 09:30','Shirley Rodriguez',1.720,1.700),
('DET-RUN-0071','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0230,'ACCEPT','2026-02-17 13:45','Shirley Rodriguez',1.840,1.800)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0072','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.4700,'REWORK','2026-02-17 09:00','Pat Candella',0.950,0.780),
('DET-RUN-0072','FINAL','Tensile Strength',450.0000,420.0000,480.0000,417.0000,'REJECT','2026-02-17 13:00','Pat Candella',0.860,0.710),
('DET-RUN-0073','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0030,'ACCEPT','2026-02-17 16:00','Shirley Rodriguez',1.590,1.560),
('DET-RUN-0073','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0070,'ACCEPT','2026-02-17 17:00','Shirley Rodriguez',1.610,1.590),
('DET-RUN-0073','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0180,'ACCEPT','2026-02-17 21:00','Shirley Rodriguez',1.490,1.460),
('DET-RUN-0074','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9940,'ACCEPT','2026-02-17 16:30','Pat Candella',1.710,1.690),
('DET-RUN-0074','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0280,'ACCEPT','2026-02-17 21:30','Pat Candella',1.830,1.780),
('DET-RUN-0075','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.0900,'ACCEPT','2026-02-17 17:30','Shirley Rodriguez',1.540,1.500),
('DET-RUN-0075','FINAL','Tensile Strength',450.0000,420.0000,480.0000,451.0000,'ACCEPT','2026-02-17 22:15','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0076','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0010,'ACCEPT','2026-02-18 00:00','Pat Candella',1.600,1.570),
('DET-RUN-0076','FINAL','Overall Length',85.0000,84.9000,85.1000,84.9920,'ACCEPT','2026-02-18 04:45','Pat Candella',1.510,1.480),
('DET-RUN-0077','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0150,'ACCEPT','2026-02-18 01:00','Shirley Rodriguez',1.630,1.590),
('DET-RUN-0077','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0210,'ACCEPT','2026-02-18 05:15','Shirley Rodriguez',1.490,1.440),
('DET-RUN-0078','FINAL','Tensile Strength',450.0000,420.0000,480.0000,449.0000,'ACCEPT','2026-02-18 06:00','Pat Candella',1.670,1.640),
('DET-RUN-0079','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0130,'ACCEPT','2026-02-18 09:00','Shirley Rodriguez',1.630,1.590)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0079','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.9200,'ACCEPT','2026-02-18 12:45','Shirley Rodriguez',1.870,1.820),
('DET-RUN-0080','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0020,'ACCEPT','2026-02-18 09:00','Pat Candella',1.720,1.700),
('DET-RUN-0080','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0240,'ACCEPT','2026-02-18 13:45','Pat Candella',1.850,1.810),
('DET-RUN-0081','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.1900,'ACCEPT','2026-02-18 09:30','Shirley Rodriguez',1.440,1.400),
('DET-RUN-0081','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,3.9800,'ACCEPT','2026-02-18 11:00','Shirley Rodriguez',1.560,1.510),
('DET-RUN-0081','FINAL','Tensile Strength',450.0000,420.0000,480.0000,446.0000,'ACCEPT','2026-02-18 14:45','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0082','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0070,'ACCEPT','2026-02-18 16:00','Pat Candella',1.590,1.560),
('DET-RUN-0082','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,5.9950,'ACCEPT','2026-02-18 17:00','Pat Candella',1.630,1.610),
('DET-RUN-0082','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0220,'ACCEPT','2026-02-18 20:45','Pat Candella',1.490,1.460),
('DET-RUN-0083','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0080,'ACCEPT','2026-02-18 16:30','Shirley Rodriguez',1.700,1.680),
('DET-RUN-0083','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0310,'ACCEPT','2026-02-18 21:45','Shirley Rodriguez',1.820,1.760),
('DET-RUN-0084','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.4800,'REWORK','2026-02-18 16:45','Pat Candella',0.950,0.780),
('DET-RUN-0084','FINAL','Tensile Strength',450.0000,420.0000,480.0000,419.0000,'REJECT','2026-02-18 21:30','Pat Candella',0.860,0.710),
('DET-RUN-0085','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0050,'ACCEPT','2026-02-23 09:00','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0085','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0080,'ACCEPT','2026-02-23 13:15','Shirley Rodriguez',1.530,1.500)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0086','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9960,'ACCEPT','2026-02-23 09:00','Pat Candella',1.720,1.700),
('DET-RUN-0086','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0210,'ACCEPT','2026-02-23 12:30','Pat Candella',1.860,1.820),
('DET-RUN-0087','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.2000,'ACCEPT','2026-02-23 09:30','Shirley Rodriguez',1.440,1.400),
('DET-RUN-0087','FINAL','Tensile Strength',450.0000,420.0000,480.0000,447.0000,'ACCEPT','2026-02-23 14:15','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0088','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0030,'ACCEPT','2026-02-23 16:00','Pat Candella',1.590,1.560),
('DET-RUN-0088','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0050,'ACCEPT','2026-02-23 17:00','Pat Candella',1.620,1.600),
('DET-RUN-0088','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0130,'ACCEPT','2026-02-23 20:45','Pat Candella',1.500,1.470),
('DET-RUN-0089','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0140,'ACCEPT','2026-02-23 16:30','Shirley Rodriguez',1.630,1.600),
('DET-RUN-0089','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.7500,'ACCEPT','2026-02-23 21:30','Shirley Rodriguez',1.890,1.850),
('DET-RUN-0090','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.0600,'ACCEPT','2026-02-23 17:00','Pat Candella',1.540,1.500),
('DET-RUN-0090','FINAL','Tensile Strength',450.0000,420.0000,480.0000,453.0000,'ACCEPT','2026-02-23 22:00','Pat Candella',1.660,1.630),
('DET-RUN-0091','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0190,'ACCEPT','2026-02-24 01:00','Shirley Rodriguez',1.600,1.540),
('DET-RUN-0091','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0270,'ACCEPT','2026-02-24 05:15','Shirley Rodriguez',1.480,1.420),
('DET-RUN-0092','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0280,'ACCEPT','2026-02-24 05:30','Pat Candella',1.830,1.780),
('DET-RUN-0093','FINAL','Tensile Strength',450.0000,420.0000,480.0000,455.0000,'ACCEPT','2026-02-24 05:45','Shirley Rodriguez',1.650,1.620)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0094','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0100,'ACCEPT','2026-02-24 09:00','Pat Candella',1.640,1.610),
('DET-RUN-0094','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0160,'ACCEPT','2026-02-24 13:15','Pat Candella',1.500,1.460),
('DET-RUN-0095','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0040,'ACCEPT','2026-02-24 09:30','Shirley Rodriguez',1.720,1.700),
('DET-RUN-0095','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0220,'ACCEPT','2026-02-24 13:45','Shirley Rodriguez',1.850,1.810),
('DET-RUN-0096','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.4600,'REWORK','2026-02-24 09:00','Pat Candella',0.960,0.790),
('DET-RUN-0096','FINAL','Tensile Strength',450.0000,420.0000,480.0000,416.0000,'REJECT','2026-02-24 13:15','Pat Candella',0.850,0.700),
('DET-RUN-0097','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0060,'ACCEPT','2026-03-02 09:00','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0097','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0090,'ACCEPT','2026-03-02 13:15','Shirley Rodriguez',1.530,1.500),
('DET-RUN-0098','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9980,'ACCEPT','2026-03-02 09:00','Pat Candella',1.720,1.700),
('DET-RUN-0098','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0200,'ACCEPT','2026-03-02 12:30','Pat Candella',1.860,1.820),
('DET-RUN-0099','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.1600,'ACCEPT','2026-03-02 09:30','Shirley Rodriguez',1.440,1.400),
('DET-RUN-0099','FINAL','Tensile Strength',450.0000,420.0000,480.0000,448.0000,'ACCEPT','2026-03-02 14:00','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0100','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0020,'ACCEPT','2026-03-02 16:00','Pat Candella',1.600,1.570),
('DET-RUN-0100','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0040,'ACCEPT','2026-03-02 17:00','Pat Candella',1.620,1.600),
('DET-RUN-0100','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0090,'ACCEPT','2026-03-02 20:45','Pat Candella',1.500,1.470)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0101','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0110,'ACCEPT','2026-03-02 16:30','Shirley Rodriguez',1.640,1.610),
('DET-RUN-0101','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.8200,'ACCEPT','2026-03-02 21:15','Shirley Rodriguez',1.880,1.840),
('DET-RUN-0102','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.0400,'ACCEPT','2026-03-02 17:00','Pat Candella',1.550,1.510),
('DET-RUN-0102','FINAL','Tensile Strength',450.0000,420.0000,480.0000,451.0000,'ACCEPT','2026-03-02 21:45','Pat Candella',1.660,1.630),
('DET-RUN-0103','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0170,'ACCEPT','2026-03-03 01:00','Shirley Rodriguez',1.620,1.580),
('DET-RUN-0103','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0240,'ACCEPT','2026-03-03 05:15','Shirley Rodriguez',1.490,1.440),
('DET-RUN-0104','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0290,'ACCEPT','2026-03-03 05:30','Pat Candella',1.820,1.770),
('DET-RUN-0105','FINAL','Tensile Strength',450.0000,420.0000,480.0000,453.0000,'ACCEPT','2026-03-03 05:45','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0106','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0080,'ACCEPT','2026-03-03 09:00','Pat Candella',1.650,1.620),
('DET-RUN-0106','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0120,'ACCEPT','2026-03-03 13:15','Pat Candella',1.510,1.470),
('DET-RUN-0107','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0030,'ACCEPT','2026-03-03 09:30','Shirley Rodriguez',1.720,1.700),
('DET-RUN-0107','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0230,'ACCEPT','2026-03-03 13:45','Shirley Rodriguez',1.840,1.800),
('DET-RUN-0108','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.4700,'REWORK','2026-03-03 09:00','Pat Candella',0.950,0.780),
('DET-RUN-0108','FINAL','Tensile Strength',450.0000,420.0000,480.0000,418.0000,'REJECT','2026-03-03 13:00','Pat Candella',0.860,0.710),
('DET-RUN-0109','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0030,'ACCEPT','2026-03-03 16:00','Shirley Rodriguez',1.590,1.560)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0109','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,6.0060,'ACCEPT','2026-03-03 17:00','Shirley Rodriguez',1.610,1.590),
('DET-RUN-0109','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0170,'ACCEPT','2026-03-03 21:00','Shirley Rodriguez',1.490,1.460),
('DET-RUN-0110','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,29.9950,'ACCEPT','2026-03-03 16:30','Pat Candella',1.710,1.690),
('DET-RUN-0110','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0270,'ACCEPT','2026-03-03 21:30','Pat Candella',1.830,1.790),
('DET-RUN-0111','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.1000,'ACCEPT','2026-03-03 17:30','Shirley Rodriguez',1.540,1.500),
('DET-RUN-0111','FINAL','Tensile Strength',450.0000,420.0000,480.0000,452.0000,'ACCEPT','2026-03-03 22:15','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0112','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0010,'ACCEPT','2026-03-04 00:00','Pat Candella',1.600,1.570),
('DET-RUN-0112','FINAL','Overall Length',85.0000,84.9000,85.1000,84.9940,'ACCEPT','2026-03-04 04:45','Pat Candella',1.510,1.480),
('DET-RUN-0113','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0160,'ACCEPT','2026-03-04 01:00','Shirley Rodriguez',1.620,1.580),
('DET-RUN-0113','FINAL','Thread Pitch Diameter',24.0000,23.9700,24.0300,24.0220,'ACCEPT','2026-03-04 05:15','Shirley Rodriguez',1.490,1.440),
('DET-RUN-0114','FINAL','Tensile Strength',450.0000,420.0000,480.0000,450.0000,'ACCEPT','2026-03-04 06:00','Pat Candella',1.670,1.640),
('DET-RUN-0115','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0120,'ACCEPT','2026-03-09 09:00','Shirley Rodriguez',1.640,1.610),
('DET-RUN-0115','FINAL','Surface Roughness',1.6000,0.8000,3.2000,1.9000,'ACCEPT','2026-03-09 12:45','Shirley Rodriguez',1.870,1.820),
('DET-RUN-0116','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0010,'ACCEPT','2026-03-09 09:00','Pat Candella',1.720,1.700),
('DET-RUN-0116','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0240,'ACCEPT','2026-03-09 13:45','Pat Candella',1.850,1.810)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0117','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.1400,'ACCEPT','2026-03-09 09:30','Shirley Rodriguez',1.450,1.410),
('DET-RUN-0117','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,3.9600,'ACCEPT','2026-03-09 11:00','Shirley Rodriguez',1.560,1.510),
('DET-RUN-0117','FINAL','Tensile Strength',450.0000,420.0000,480.0000,447.0000,'ACCEPT','2026-03-09 14:45','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0118','SPC_STUDY','Slot Width',12.0000,11.9500,12.0500,12.0050,'ACCEPT','2026-03-09 16:00','Pat Candella',1.590,1.560),
('DET-RUN-0118','SPC_STUDY','Slot Depth',6.0000,5.9700,6.0300,5.9970,'ACCEPT','2026-03-09 17:00','Pat Candella',1.630,1.610),
('DET-RUN-0118','FINAL','Overall Length',85.0000,84.9000,85.1000,85.0200,'ACCEPT','2026-03-09 20:45','Pat Candella',1.490,1.460),
('DET-RUN-0119','IN_PROCESS','Turned OD',30.0000,29.9700,30.0300,30.0060,'ACCEPT','2026-03-09 16:30','Shirley Rodriguez',1.710,1.690),
('DET-RUN-0119','FINAL','Concentricity',0.0000,0.0000,0.0500,0.0290,'ACCEPT','2026-03-09 21:45','Shirley Rodriguez',1.820,1.760),
('DET-RUN-0120','IN_PROCESS','Weld Bead Width',8.0000,7.5000,8.5000,8.0900,'ACCEPT','2026-03-09 16:30','Pat Candella',1.460,1.430),
('DET-RUN-0120','IN_PROCESS','Weld Penetration Depth',4.0000,3.5000,4.5000,4.0300,'ACCEPT','2026-03-09 18:00','Pat Candella',1.550,1.510),
('DET-RUN-0120','FINAL','Tensile Strength',450.0000,420.0000,480.0000,452.0000,'ACCEPT','2026-03-09 21:45','Pat Candella',1.660,1.630),
-- SPC_STUDY deep-dive: CNC-MILL-04 process capability study across Feb-Mar
('DET-RUN-0001','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0040,'ACCEPT','2026-02-02 07:00','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0001','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0060,'ACCEPT','2026-02-02 07:30','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0013','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0030,'ACCEPT','2026-02-03 15:00','Shirley Rodriguez',1.690,1.660),
('DET-RUN-0013','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0050,'ACCEPT','2026-02-03 15:30','Shirley Rodriguez',1.680,1.650)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0025','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0020,'ACCEPT','2026-02-04 23:00','Shirley Rodriguez',1.700,1.670),
('DET-RUN-0025','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0040,'ACCEPT','2026-02-04 23:30','Shirley Rodriguez',1.690,1.660),
('DET-RUN-0049','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0010,'ACCEPT','2026-02-10 15:00','Shirley Rodriguez',1.710,1.680),
('DET-RUN-0049','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0030,'ACCEPT','2026-02-10 15:30','Shirley Rodriguez',1.700,1.670),
('DET-RUN-0061','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0050,'ACCEPT','2026-02-16 08:00','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0061','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0070,'ACCEPT','2026-02-16 08:30','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0073','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0020,'ACCEPT','2026-02-17 15:00','Shirley Rodriguez',1.700,1.670),
('DET-RUN-0073','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0040,'ACCEPT','2026-02-17 15:30','Shirley Rodriguez',1.690,1.660),
('DET-RUN-0085','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0030,'ACCEPT','2026-02-23 08:00','Shirley Rodriguez',1.700,1.670),
('DET-RUN-0085','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0050,'ACCEPT','2026-02-23 08:30','Shirley Rodriguez',1.690,1.660),
('DET-RUN-0097','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0040,'ACCEPT','2026-03-02 08:00','Shirley Rodriguez',1.690,1.660),
('DET-RUN-0097','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0060,'ACCEPT','2026-03-02 08:30','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0109','SPC_STUDY','Bore Diameter Subgroup 1',25.0000,24.9800,25.0200,25.0020,'ACCEPT','2026-03-03 15:00','Shirley Rodriguez',1.710,1.680),
('DET-RUN-0109','SPC_STUDY','Bore Diameter Subgroup 2',25.0000,24.9800,25.0200,25.0040,'ACCEPT','2026-03-03 15:30','Shirley Rodriguez',1.700,1.670),
-- CNC-LATHE-02 turned OD SPC study
('DET-RUN-0002','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,30.0010,'ACCEPT','2026-02-02 07:00','Pat Candella',1.730,1.710)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0002','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,30.0030,'ACCEPT','2026-02-02 07:30','Pat Candella',1.720,1.700),
('DET-RUN-0014','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,29.9990,'ACCEPT','2026-02-03 15:00','Pat Candella',1.740,1.720),
('DET-RUN-0014','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,30.0020,'ACCEPT','2026-02-03 15:30','Pat Candella',1.730,1.710),
('DET-RUN-0026','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,30.0050,'ACCEPT','2026-02-04 23:00','Pat Candella',1.710,1.690),
('DET-RUN-0026','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,30.0070,'ACCEPT','2026-02-04 23:30','Pat Candella',1.700,1.680),
('DET-RUN-0050','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,29.9960,'ACCEPT','2026-02-10 15:00','Pat Candella',1.740,1.720),
('DET-RUN-0050','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,29.9980,'ACCEPT','2026-02-10 15:30','Pat Candella',1.730,1.710),
('DET-RUN-0062','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,30.0010,'ACCEPT','2026-02-16 08:00','Pat Candella',1.730,1.710),
('DET-RUN-0062','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,30.0030,'ACCEPT','2026-02-16 08:30','Pat Candella',1.720,1.700),
('DET-RUN-0074','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,29.9970,'ACCEPT','2026-02-17 15:00','Pat Candella',1.740,1.720),
('DET-RUN-0074','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,29.9990,'ACCEPT','2026-02-17 15:30','Pat Candella',1.730,1.710),
('DET-RUN-0086','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,30.0020,'ACCEPT','2026-02-23 08:00','Pat Candella',1.730,1.710),
('DET-RUN-0086','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,30.0040,'ACCEPT','2026-02-23 08:30','Pat Candella',1.720,1.700),
('DET-RUN-0098','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,30.0000,'ACCEPT','2026-03-02 08:00','Pat Candella',1.740,1.720),
('DET-RUN-0098','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,30.0020,'ACCEPT','2026-03-02 08:30','Pat Candella',1.730,1.710)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0110','SPC_STUDY','Turned OD Subgroup 1',30.0000,29.9700,30.0300,29.9950,'ACCEPT','2026-03-03 15:00','Pat Candella',1.740,1.720),
('DET-RUN-0110','SPC_STUDY','Turned OD Subgroup 2',30.0000,29.9700,30.0300,29.9970,'ACCEPT','2026-03-03 15:30','Pat Candella',1.730,1.710),
-- ROBOT-WELD-01 tensile strength SPC study
('DET-RUN-0003','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,449.0000,'ACCEPT','2026-02-02 10:00','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0003','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,451.0000,'ACCEPT','2026-02-02 11:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0015','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,452.0000,'ACCEPT','2026-02-03 17:00','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0015','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,448.0000,'ACCEPT','2026-02-03 18:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0027','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,450.0000,'ACCEPT','2026-02-04 23:30','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0027','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,453.0000,'ACCEPT','2026-02-05 00:30','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0051','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,455.0000,'ACCEPT','2026-02-10 17:00','Shirley Rodriguez',1.650,1.620),
('DET-RUN-0051','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,447.0000,'ACCEPT','2026-02-10 18:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0063','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,449.0000,'ACCEPT','2026-02-16 10:00','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0063','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,451.0000,'ACCEPT','2026-02-16 11:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0075','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,452.0000,'ACCEPT','2026-02-17 17:00','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0075','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,448.0000,'ACCEPT','2026-02-17 18:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0087','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,450.0000,'ACCEPT','2026-02-23 10:00','Shirley Rodriguez',1.680,1.650)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0087','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,453.0000,'ACCEPT','2026-02-23 11:00','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0099','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,449.0000,'ACCEPT','2026-03-02 10:00','Shirley Rodriguez',1.680,1.650),
('DET-RUN-0099','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,451.0000,'ACCEPT','2026-03-02 11:00','Shirley Rodriguez',1.670,1.640),
('DET-RUN-0111','SPC_STUDY','Tensile Strength Subgroup 1',450.0000,420.0000,480.0000,452.0000,'ACCEPT','2026-03-03 17:00','Shirley Rodriguez',1.660,1.630),
('DET-RUN-0111','SPC_STUDY','Tensile Strength Subgroup 2',450.0000,420.0000,480.0000,448.0000,'ACCEPT','2026-03-03 18:00','Shirley Rodriguez',1.670,1.640),
-- DEVIATION records: process adjustments logged during runs
('DET-RUN-0010','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0195,'DEVIATION','2026-02-03 10:00','Shirley Rodriguez',1.020,0.980),
('DET-RUN-0031','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0198,'DEVIATION','2026-02-05 17:30','Shirley Rodriguez',1.010,0.970),
('DET-RUN-0053','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0196,'DEVIATION','2026-02-11 02:00','Shirley Rodriguez',1.020,0.980),
('DET-RUN-0077','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0197,'DEVIATION','2026-02-18 02:00','Shirley Rodriguez',1.010,0.970),
('DET-RUN-0101','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0194,'DEVIATION','2026-03-02 17:30','Shirley Rodriguez',1.020,0.980),
('DET-RUN-0113','IN_PROCESS','Bore Diameter',25.0000,24.9800,25.0200,25.0196,'DEVIATION','2026-03-04 02:00','Shirley Rodriguez',1.010,0.970),
-- REWORK follow-up inspections after weld cell corrections
('DET-RUN-0024','FINAL','Weld Bead Width Rework',8.0000,7.5000,8.5000,8.2500,'ACCEPT','2026-02-04 23:00','Pat Candella',1.380,1.340),
('DET-RUN-0048','FINAL','Weld Bead Width Rework',8.0000,7.5000,8.5000,8.2200,'ACCEPT','2026-02-10 15:30','Pat Candella',1.400,1.360),
('DET-RUN-0060','FINAL','Weld Bead Width Rework',8.0000,7.5000,8.5000,8.2800,'ACCEPT','2026-02-11 23:30','Pat Candella',1.370,1.330),
('DET-RUN-0072','FINAL','Weld Bead Width Rework',8.0000,7.5000,8.5000,8.2100,'ACCEPT','2026-02-17 15:00','Pat Candella',1.410,1.370),
('DET-RUN-0084','FINAL','Weld Bead Width Rework',8.0000,7.5000,8.5000,8.2400,'ACCEPT','2026-02-18 23:30','Pat Candella',1.390,1.350),
('DET-RUN-0096','FINAL','Weld Bead Width Rework',8.0000,7.5000,8.5000,8.2300,'ACCEPT','2026-02-24 15:00','Pat Candella',1.400,1.360),
('DET-RUN-0108','FINAL','Weld Bead Width Rework',8.0000,7.5000,8.5000,8.2600,'ACCEPT','2026-03-03 15:00','Pat Candella',1.380,1.340),
-- Additional INCOMING checks for raw material traceability
('DET-RUN-0037','INCOMING','Material Hardness',58.0000,55.0000,62.0000,57.8000,'ACCEPT','2026-02-09 05:45','Shirley Rodriguez',1.560,1.530),
('DET-RUN-0061','INCOMING','Material Hardness',58.0000,55.0000,62.0000,58.2000,'ACCEPT','2026-02-16 05:45','Shirley Rodriguez',1.550,1.520),
('DET-RUN-0085','INCOMING','Material Hardness',58.0000,55.0000,62.0000,57.5000,'ACCEPT','2026-02-23 05:45','Shirley Rodriguez',1.570,1.540),
('DET-RUN-0097','INCOMING','Material Hardness',58.0000,55.0000,62.0000,58.1000,'ACCEPT','2026-03-02 05:45','Shirley Rodriguez',1.560,1.530),
('DET-RUN-0115','INCOMING','Material Hardness',58.0000,55.0000,62.0000,57.9000,'ACCEPT','2026-03-09 05:45','Shirley Rodriguez',1.560,1.530)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));

INSERT INTO quality_records (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
SELECT v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)
FROM (VALUES
('DET-RUN-0005','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5200,'ACCEPT','2026-02-02 15:00','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0005','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6800,'ACCEPT','2026-02-02 15:30','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0017','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5500,'ACCEPT','2026-02-04 00:00','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0017','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6500,'ACCEPT','2026-02-04 00:30','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0029','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5800,'ACCEPT','2026-02-05 08:00','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0029','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6200,'ACCEPT','2026-02-05 08:30','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0041','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5400,'ACCEPT','2026-02-09 15:00','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0041','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6600,'ACCEPT','2026-02-09 15:30','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0053','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5700,'ACCEPT','2026-02-11 00:00','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0053','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6300,'ACCEPT','2026-02-11 00:30','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0065','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5300,'ACCEPT','2026-02-16 15:00','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0065','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6700,'ACCEPT','2026-02-16 15:30','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0077','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5600,'ACCEPT','2026-02-18 00:00','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0077','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6400,'ACCEPT','2026-02-18 00:30','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0089','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5500,'ACCEPT','2026-02-23 15:00','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0089','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6500,'ACCEPT','2026-02-23 15:30','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0101','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5800,'ACCEPT','2026-03-02 15:00','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0101','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6200,'ACCEPT','2026-03-02 15:30','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0113','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5400,'ACCEPT','2026-03-04 00:00','Shirley Rodriguez',1.910,1.870),
('DET-RUN-0113','SPC_STUDY','Surface Roughness Subgroup 2',1.6000,0.8000,3.2000,1.6600,'ACCEPT','2026-03-04 00:30','Shirley Rodriguez',1.900,1.860),
('DET-RUN-0115','SPC_STUDY','Surface Roughness Subgroup 1',1.6000,0.8000,3.2000,1.5700,'ACCEPT','2026-03-09 08:00','Shirley Rodriguez',1.900,1.860)
) AS v (run_number, inspection_type, characteristic, target_value, lower_limit, upper_limit, actual_value, disposition, inspection_time, inspector_name, cp_value, cpk_value)
WHERE NOT EXISTS (SELECT 1 FROM quality_records t WHERE ROW(t.run_number, t.inspection_type, t.characteristic, t.target_value, t.lower_limit, t.upper_limit, t.actual_value, t.disposition, t.inspection_time, t.inspector_name, t.cp_value, t.cpk_value) IS NOT DISTINCT FROM ROW(v.run_number, v.inspection_type, v.characteristic, v.target_value::NUMERIC(10,4), v.lower_limit::NUMERIC(10,4), v.upper_limit::NUMERIC(10,4), v.actual_value::NUMERIC(10,4), v.disposition, v.inspection_time::TIMESTAMP, v.inspector_name, v.cp_value::NUMERIC(6,3), v.cpk_value::NUMERIC(6,3)));
